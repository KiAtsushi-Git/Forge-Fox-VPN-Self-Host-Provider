mod models;

use axum::{
    extract::{Path, Request, State},
    http::{header, StatusCode},
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{delete, get, post},
    Router, Json,
};
use serde::{Deserialize, Serialize};
use sqlx::{any::AnyPoolOptions, AnyPool};
use std::collections::HashMap;
use std::net::SocketAddr;
use std::sync::Arc;
use std::time::{Duration, Instant};
use tower_http::services::{ServeDir, ServeFile};
use models::{Node, Client};

const SESSION_TTL: Duration = Duration::from_secs(24 * 60 * 60);
const SSH_PROVISION_TIMEOUT: Duration = Duration::from_secs(30);
const UPDATE_REPO: &str = "KiAtsushi-Git/Forge-Fox-VPN-Self-Host-Provider";

#[derive(Clone)]
struct AppState {
    db: AnyPool,
    // token -> expiry
    sessions: Arc<tokio::sync::Mutex<HashMap<String, Instant>>>,
}

#[derive(Serialize)]
struct StatusResponse {
    status: &'static str,
    nodes_count: i64,
    clients_count: i64,
}

async fn get_dashboard(State(state): State<AppState>) -> Json<StatusResponse> {
    let nodes_count: (i64,) = sqlx::query_as("SELECT COUNT(*) FROM nodes")
        .fetch_one(&state.db)
        .await
        .unwrap_or((0,));

    let clients_count: (i64,) = sqlx::query_as("SELECT COUNT(*) FROM clients")
        .fetch_one(&state.db)
        .await
        .unwrap_or((0,));

    Json(StatusResponse {
        status: "ok",
        nodes_count: nodes_count.0,
        clients_count: clients_count.0,
    })
}

async fn get_nodes(State(state): State<AppState>) -> Json<Vec<Node>> {
    let nodes = sqlx::query_as::<_, Node>("SELECT id, name, ip, port, ssh_user, COALESCE(ssh_pass, '') AS ssh_pass, status, CAST(created_at AS TEXT) AS created_at FROM nodes")
        .fetch_all(&state.db)
        .await
        .unwrap_or_default();
    Json(nodes)
}

// Payloads for the create endpoints. They must NOT reuse the persisted models:
// `Node`/`Client` derive Deserialize with a required `id`, and the dashboard
// form has no id to send — axum answered every POST with a 422 that the UI
// silently swallowed.

#[derive(Deserialize)]
struct NewNode {
    name: String,
    ip: String,
    port: Option<i64>,
    ssh_user: Option<String>,
    ssh_pass: Option<String>,
}

async fn add_node(State(state): State<AppState>, Json(payload): Json<NewNode>) -> Response {
    let name = payload.name.trim().to_string();
    let ip = payload.ip.trim().to_string();
    if name.is_empty() || ip.is_empty() {
        return (
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({ "error": "Укажите название и IP ноды" })),
        )
            .into_response();
    }
    let port = match payload.port {
        Some(p) if p > 0 && p <= 65535 => p,
        _ => 22,
    };
    let ssh_user = payload
        .ssh_user
        .and_then(|u| (!u.trim().is_empty()).then(|| u.trim().to_string()))
        .unwrap_or_else(|| "root".to_string());

    let id = uuid::Uuid::new_v4().to_string();
    let insert = sqlx::query(
        "INSERT INTO nodes (id, name, ip, port, ssh_user, ssh_pass, status) VALUES ($1, $2, $3, $4, $5, $6, $7)",
    )
    .bind(&id)
    .bind(&name)
    .bind(&ip)
    .bind(port)
    .bind(&ssh_user)
    .bind(&payload.ssh_pass)
    .bind("online")
    .execute(&state.db)
    .await;

    if let Err(e) = insert {
        tracing::error!("Failed to insert node: {e}");
        return (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(serde_json::json!({ "error": "Ошибка БД при сохранении ноды" })),
        )
            .into_response();
    }

    let new_node = sqlx::query_as::<_, Node>("SELECT id, name, ip, port, ssh_user, COALESCE(ssh_pass, '') AS ssh_pass, status, CAST(created_at AS TEXT) AS created_at FROM nodes WHERE id = $1")
        .bind(&id)
        .fetch_one(&state.db)
        .await;

    match new_node {
        Ok(n) => Json(n).into_response(),
        Err(e) => {
            tracing::error!("Failed to re-read node: {e}");
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(serde_json::json!({ "error": "Ошибка БД при чтении ноды" })),
            )
                .into_response()
        }
    }
}

async fn get_clients(State(state): State<AppState>) -> Json<Vec<Client>> {
    let clients = sqlx::query_as::<_, Client>("SELECT id, username, node_id, COALESCE(node_ids, '') AS node_ids, COALESCE(password, '') AS password, CAST(expiry AS TEXT) AS expiry, limit_gb, used_bytes, CAST(created_at AS TEXT) AS created_at FROM clients")
        .fetch_all(&state.db)
        .await
        .unwrap_or_default();
    Json(clients)
}

#[derive(Deserialize)]
struct NewClient {
    username: String,
    /// Old UI builds send a single node_id; new ones send node_ids[].
    node_id: Option<String>,
    node_ids: Option<Vec<String>>,
    /// Kept as a string: the dashboard's `datetime-local` input sends
    /// "YYYY-MM-DDTHH:MM" (no seconds), which chrono's NaiveDateTime
    /// deserializer rejects.
    expiry: Option<String>,
    limit_gb: Option<i64>,
}

/// Parse the expiry formats the dashboard can produce; None keeps it unlimited.
fn parse_expiry(raw: &str) -> Result<Option<chrono::NaiveDateTime>, String> {
    let raw = raw.trim();
    if raw.is_empty() {
        return Ok(None);
    }
    const FORMATS: [&str; 3] = ["%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"];
    for fmt in FORMATS {
        if let Ok(dt) = chrono::NaiveDateTime::parse_from_str(raw, fmt) {
            return Ok(Some(dt));
        }
    }
    Err(format!("Не удалось разобрать дату: {raw} (ожидается ГГГГ-ММ-ДД ЧЧ:ММ)"))
}

async fn add_client(
    State(state): State<AppState>,
    Json(payload): Json<NewClient>,
) -> Response {
    // The username becomes a system SSH user on the node — validate it strictly
    let username = payload.username.trim().to_string();
    let valid = !username.is_empty()
        && username.len() <= 32
        && username
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_');
    if !valid {
        return (
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({ "error": "Имя пользователя: 1-32 символа, только латиница, цифры, - и _" })),
        )
            .into_response();
    }

    let expiry = match payload.expiry.as_deref().map(parse_expiry) {
        Some(Err(msg)) => {
            return (
                StatusCode::BAD_REQUEST,
                Json(serde_json::json!({ "error": msg })),
            )
                .into_response()
        }
        Some(Ok(exp)) => exp,
        None => None,
    };

    // Resolve the target nodes: the new UI sends node_ids[], old builds
    // send node_id. Deduplicate while keeping order.
    let mut wanted: Vec<String> = payload.node_ids.clone().unwrap_or_default();
    if let Some(single) = payload.node_id.as_deref().filter(|s| !s.is_empty()) {
        if !wanted.iter().any(|w| w == single) {
            wanted.push(single.to_string());
        }
    }
    wanted.retain(|s| !s.is_empty());
    if wanted.is_empty() {
        return (
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({ "error": "Выберите хотя бы одну ноду" })),
        )
            .into_response();
    }

    // Load every requested node
    let mut nodes: Vec<Node> = Vec::new();
    for node_id in &wanted {
        match sqlx::query_as::<_, Node>("SELECT id, name, ip, port, ssh_user, COALESCE(ssh_pass, '') AS ssh_pass, status, CAST(created_at AS TEXT) AS created_at FROM nodes WHERE id = $1")
            .bind(node_id)
            .fetch_optional(&state.db)
            .await
        {
            Ok(Some(n)) => nodes.push(n),
            Ok(None) => {
                return (
                    StatusCode::BAD_REQUEST,
                    Json(serde_json::json!({ "error": "Нода не найдена" })),
                )
                    .into_response()
            }
            Err(e) => {
                tracing::error!("DB error fetching node: {e}");
                return (
                    StatusCode::INTERNAL_SERVER_ERROR,
                    Json(serde_json::json!({ "error": "Ошибка БД" })),
                )
                    .into_response()
            }
        }
    }

    // Create the user on every node via SSH — one shared password so the
    // subscription link works on whichever node the client connects to.
    // On failure, roll back the users already created so a retry starts clean.
    let password = gen_password(20);
    let mut created_on: Vec<&Node> = Vec::new();
    for node in &nodes {
        if let Err(e) = provision_user_on_node(node, &username, &password).await {
            tracing::error!("Provisioning failed on node {}: {e}", node.name);
            for done in created_on {
                let _ = run_node_command(
                    done,
                    &format!("id -u '{u}' >/dev/null 2>&1 && userdel -r '{u}' || true", u = username),
                )
                .await;
            }
            return (
                StatusCode::BAD_GATEWAY,
                Json(serde_json::json!({ "error": format!("Не удалось создать пользователя на ноде {}: {}", node.name, e) })),
            )
                .into_response();
        }
        created_on.push(node);
    }

    let node_ids_csv = wanted.join(",");
    let primary_node_id = wanted[0].clone();
    let id = uuid::Uuid::new_v4().to_string();
    // Bind expiry as a plain TEXT->TIMESTAMP cast that works on both backends.
    // The old `CAST($5 AS TIMESTAMP)` broke on PostgreSQL: when expiry is
    // None, sqlx types the NULL parameter as integer and Postgres rejects
    // "cannot cast type integer to timestamp without time zone".
    if let Err(e) = sqlx::query(
        "INSERT INTO clients (id, username, node_id, node_ids, password, expiry, limit_gb, used_bytes) \
         VALUES ($1, $2, $3, $7, $4, CAST(NULLIF($5, '') AS TIMESTAMP), $6, 0)",
    )
    .bind(&id)
    .bind(&username)
    .bind(&primary_node_id)
    .bind(&password)
    .bind(&node_ids_csv)
    .bind(expiry.map(|dt| dt.format("%Y-%m-%d %H:%M:%S").to_string()).unwrap_or_default())
    .bind(payload.limit_gb)
    .execute(&state.db)
    .await
    {
        tracing::error!("Failed to insert client: {e}");
        return (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(serde_json::json!({ "error": "Ошибка БД при сохранении клиента" })),
        )
            .into_response();
    }

    let new_client = sqlx::query_as::<_, Client>("SELECT id, username, node_id, COALESCE(node_ids, '') AS node_ids, COALESCE(password, '') AS password, CAST(expiry AS TEXT) AS expiry, limit_gb, used_bytes, CAST(created_at AS TEXT) AS created_at FROM clients WHERE id = $1")
        .bind(&id)
        .fetch_one(&state.db)
        .await
        .unwrap();

    Json(new_client).into_response()
}

// ── Node provisioning (SSH) ─────────────────────────────────────────────────

/// Random password from an unambiguous alphanumeric set.
fn gen_password(len: usize) -> String {
    const CHARS: &[u8] = b"ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789";
    (0..len)
        .map(|_| CHARS[(rand::random::<u64>() % CHARS.len() as u64) as usize] as char)
        .collect()
}

struct NodeSshHandler;

#[async_trait::async_trait]
impl russh::client::Handler for NodeSshHandler {
    type Error = russh::Error;

    async fn check_server_key(
        self,
        _server_public_key: &russh_keys::key::PublicKey,
    ) -> Result<(Self, bool), Self::Error> {
        // TODO: pin host keys per node instead of accepting everything
        Ok((self, true))
    }
}

/// Create (or update) the VPN user on the node via SSH.
///
/// Mirrors the desktop client's Host install: users go into the `forgefox`
/// group with the restricted `ff-shell`; if the Host install hasn't been done,
/// falls back to a normal shell.
async fn provision_user_on_node(node: &Node, username: &str, password: &str) -> Result<(), String> {
    let ssh_pass = node
        .ssh_pass
        .as_deref()
        .ok_or_else(|| "у ноды не задан SSH пароль".to_string())?;

    let fut = async {
        let config = Arc::new(russh::client::Config::default());
        let mut session = russh::client::connect(
            config,
            (node.ip.as_str(), u16::try_from(node.port).unwrap_or(22)),
            NodeSshHandler,
        )
        .await
        .map_err(|e| format!("SSH connect: {e}"))?;

        let authed = session
            .authenticate_password(&node.ssh_user, ssh_pass)
            .await
            .map_err(|e| format!("SSH auth: {e}"))?;
        if !authed {
            return Err("SSH auth: неверный логин/пароль ноды".to_string());
        }

        // username is validated (alnum/-/_) and the password is generated
        // from a safe alphabet, so single-quoting here is injection-safe
        let script = format!(
            r#"groupadd -f forgefox
SHELL_PATH=/usr/local/bin/ff-shell
[ -f "$SHELL_PATH" ] || SHELL_PATH=/bin/bash
if ! id -u '{user}' >/dev/null 2>&1; then
  useradd -m -g forgefox -s "$SHELL_PATH" '{user}'
fi
echo '{user}:{pass}' | chpasswd
echo PROVISION_OK"#,
            user = username,
            pass = password
        );

        let mut channel = session
            .channel_open_session()
            .await
            .map_err(|e| format!("SSH channel: {e}"))?;
        channel
            .exec(true, script)
            .await
            .map_err(|e| format!("SSH exec: {e}"))?;

        let mut output = String::new();
        let mut exit_code: u32 = 0;
        while let Some(msg) = channel.wait().await {
            match msg {
                russh::ChannelMsg::Data { ref data } => {
                    output.push_str(&String::from_utf8_lossy(data));
                }
                russh::ChannelMsg::ExitStatus { exit_status: code } => {
                    exit_code = code;
                }
                _ => {}
            }
        }

        let _ = session
            .disconnect(russh::Disconnect::ByApplication, "done", "en")
            .await;

        if exit_code != 0 {
            return Err(format!("exit code {exit_code}: {output}"));
        }
        if !output.contains("PROVISION_OK") {
            return Err(format!("неожиданный ответ: {output}"));
        }
        Ok(())
    };

    tokio::time::timeout(SSH_PROVISION_TIMEOUT, fut)
        .await
        .map_err(|_| "таймаут SSH".to_string())?
}

// ── Auth ─────────────────────────────────────────────────────────────────────

#[derive(Deserialize)]
struct LoginRequest {
    username: String,
    password: String,
}

#[derive(Serialize)]
struct LoginResponse {
    token: String,
    username: String,
}

/// POST /api/login — issue a session token for valid credentials.
async fn login(State(state): State<AppState>, Json(payload): Json<LoginRequest>) -> Response {
    let admin: Option<(String, String)> =
        sqlx::query_as("SELECT username, password_hash FROM admins WHERE username = $1")
            .bind(&payload.username)
            .fetch_optional(&state.db)
            .await
            .unwrap_or(None);

    let valid = match &admin {
        Some((_, hash)) => bcrypt::verify(&payload.password, hash).unwrap_or(false),
        None => false,
    };

    if !valid {
        tracing::warn!("Failed login attempt for user '{}'", payload.username);
        return (
            StatusCode::UNAUTHORIZED,
            Json(serde_json::json!({ "error": "Неверный логин или пароль" })),
        )
            .into_response();
    }

    let token = uuid::Uuid::new_v4().to_string();
    state
        .sessions
        .lock()
        .await
        .insert(token.clone(), Instant::now() + SESSION_TTL);

    tracing::info!("Admin '{}' logged in", payload.username);
    Json(LoginResponse { token, username: payload.username }).into_response()
}

/// Middleware: require a valid `Authorization: Bearer <token>` on /api/* routes
/// (mounted after /api/login, which stays public).
async fn auth_middleware(
    State(state): State<AppState>,
    req: Request,
    next: Next,
) -> Result<Response, StatusCode> {
    let token = req
        .headers()
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
        .map(|s| s.to_string());

    let token = match token {
        Some(t) => t,
        None => return Err(StatusCode::UNAUTHORIZED),
    };

    let mut sessions = state.sessions.lock().await;
    match sessions.get(&token) {
        Some(expiry) if *expiry > Instant::now() => Ok(next.run(req).await),
        _ => {
            sessions.remove(&token);
            Err(StatusCode::UNAUTHORIZED)
        }
    }
}

// ── Subscription (public, consumed by VPN clients) ──────────────────────────

/// Percent-encode a URI fragment component (server names may contain spaces etc.)
fn encode_fragment(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for b in s.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'.' | b'_' | b'~' => out.push(b as char),
            _ => out.push_str(&format!("%{:02X}", b)),
        }
    }
    out
}

/// GET /sub/:id — plain-text subscription in `ssh://user:pass@host:port#name`
/// format (the format the ForgeFox desktop and Android clients parse).
async fn get_subscription(
    State(state): State<AppState>,
    Path(id): Path<String>,
) -> Response {
    let client = sqlx::query_as::<_, Client>("SELECT id, username, node_id, COALESCE(node_ids, '') AS node_ids, COALESCE(password, '') AS password, CAST(expiry AS TEXT) AS expiry, limit_gb, used_bytes, CAST(created_at AS TEXT) AS created_at FROM clients WHERE id = $1")
        .bind(&id)
        .fetch_optional(&state.db)
        .await
        .unwrap_or(None);

    let client = match client {
        Some(c) => c,
        None => return (StatusCode::NOT_FOUND, "Client not found").into_response(),
    };

    let nodes = sqlx::query_as::<_, Node>("SELECT id, name, ip, port, ssh_user, COALESCE(ssh_pass, '') AS ssh_pass, status, CAST(created_at AS TEXT) AS created_at FROM nodes")
        .fetch_all(&state.db)
        .await
        .unwrap_or_default();

    let wanted = client.all_node_ids();
    let client_nodes: Vec<&Node> = nodes.iter().filter(|n| wanted.contains(&n.id)).collect();
    if client_nodes.is_empty() {
        return (StatusCode::NOT_FOUND, "Node not found").into_response();
    }

    // Note: if provisioning failed the client was never stored, so the
    // password here is the real one generated and set on the node(s).
    let mut body = String::new();
    for node in client_nodes {
        let name = format!("{} ({})", node.name, client.username);
        body.push_str(&format!(
            "ssh://{}:{}@{}:{}#{}
",
            client.username,
            client.password.as_deref().unwrap_or("generated_password_here"),
            node.ip,
            node.port,
            encode_fragment(&name)
        ));
    }

    (
        [(header::CONTENT_TYPE, "text/plain; charset=utf-8")],
        body,
    )
        .into_response()
}



// ── Node / Client management (edit, delete) ─────────────────────────────────

#[derive(Deserialize)]
struct NodeUpdate {
    name: Option<String>,
    ip: Option<String>,
    port: Option<i64>,
    ssh_user: Option<String>,
    ssh_pass: Option<String>,
}

/// PUT /api/nodes/:id — update editable node fields (None = keep current).
async fn update_node(
    State(state): State<AppState>,
    Path(id): Path<String>,
    Json(payload): Json<NodeUpdate>,
) -> Response {
    let name = payload.name.map(|s| s.trim().to_string()).filter(|s| !s.is_empty());
    let ip = payload.ip.map(|s| s.trim().to_string()).filter(|s| !s.is_empty());
    if let Some(p) = payload.port {
        if !(1..=65535).contains(&p) {
            return (StatusCode::BAD_REQUEST, Json(serde_json::json!({ "error": "Порт должен быть 1-65535" }))).into_response();
        }
    }

    let res = sqlx::query(
        "UPDATE nodes SET \
         name = COALESCE($1, name), \
         ip = COALESCE($2, ip), \
         port = COALESCE($3, port), \
         ssh_user = COALESCE($4, ssh_user), \
         ssh_pass = COALESCE($5, ssh_pass) \
         WHERE id = $6",
    )
    .bind(&name)
    .bind(&ip)
    .bind(payload.port)
    .bind(payload.ssh_user.as_deref().map(str::trim).map(|s| s.to_string()).filter(|s| !s.is_empty()))
    .bind(&payload.ssh_pass)
    .bind(&id)
    .execute(&state.db)
    .await;

    match res {
        Ok(r) if r.rows_affected() > 0 => {
            audit(&state, &format!("Нода {} обновлена", id)).await;
            let node = sqlx::query_as::<_, Node>("SELECT id, name, ip, port, ssh_user, COALESCE(ssh_pass, '') AS ssh_pass, status, CAST(created_at AS TEXT) AS created_at FROM nodes WHERE id = $1")
                .bind(&id)
                .fetch_one(&state.db)
                .await;
            match node {
                Ok(n) => Json(n).into_response(),
                Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, Json(serde_json::json!({ "error": format!("БД: {e}") }))).into_response(),
            }
        }
        Ok(_) => (StatusCode::NOT_FOUND, Json(serde_json::json!({ "error": "Нода не найдена" }))).into_response(),
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, Json(serde_json::json!({ "error": format!("БД: {e}") }))).into_response(),
    }
}

/// DELETE /api/nodes/:id — remove a node and its clients (cascade).
async fn delete_node(State(state): State<AppState>, Path(id): Path<String>) -> Response {
    let res = sqlx::query("DELETE FROM nodes WHERE id = $1")
        .bind(&id)
        .execute(&state.db)
        .await;
    match res {
        Ok(r) if r.rows_affected() > 0 => {
            audit(&state, &format!("Нода {} удалена", id)).await;
            StatusCode::NO_CONTENT.into_response()
        }
        Ok(_) => (StatusCode::NOT_FOUND, Json(serde_json::json!({ "error": "Нода не найдена" }))).into_response(),
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, Json(serde_json::json!({ "error": format!("БД: {e}") }))).into_response(),
    }
}

/// DELETE /api/clients/:id — remove a client and the SSH user from its node.
async fn delete_client(State(state): State<AppState>, Path(id): Path<String>) -> Response {
    let client = match sqlx::query_as::<_, Client>("SELECT id, username, node_id, COALESCE(node_ids, '') AS node_ids, COALESCE(password, '') AS password, CAST(expiry AS TEXT) AS expiry, limit_gb, used_bytes, CAST(created_at AS TEXT) AS created_at FROM clients WHERE id = $1")
        .bind(&id)
        .fetch_optional(&state.db)
        .await
    {
        Ok(Some(c)) => c,
        Ok(None) => return (StatusCode::NOT_FOUND, Json(serde_json::json!({ "error": "Клиент не найден" }))).into_response(),
        Err(e) => return (StatusCode::INTERNAL_SERVER_ERROR, Json(serde_json::json!({ "error": format!("БД: {e}") }))).into_response(),
    };

    // Best-effort removal of the system user from every node it lives on
    let all_nodes = sqlx::query_as::<_, Node>("SELECT id, name, ip, port, ssh_user, COALESCE(ssh_pass, '') AS ssh_pass, status, CAST(created_at AS TEXT) AS created_at FROM nodes")
        .fetch_all(&state.db)
        .await
        .unwrap_or_default();
    let wanted = client.all_node_ids();
    for node in all_nodes.iter().filter(|n| wanted.contains(&n.id)) {
        let _ = run_node_command(node, &format!("id -u '{u}' >/dev/null 2>&1 && userdel -r '{u}' || true", u = client.username)).await;
    }

    match sqlx::query("DELETE FROM clients WHERE id = $1").bind(&id).execute(&state.db).await {
        Ok(r) if r.rows_affected() > 0 => {
            audit(&state, &format!("Клиент {} удалён", client.username)).await;
            StatusCode::NO_CONTENT.into_response()
        }
        Ok(_) => (StatusCode::NOT_FOUND, Json(serde_json::json!({ "error": "Клиент не найден" }))).into_response(),
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, Json(serde_json::json!({ "error": format!("БД: {e}") }))).into_response(),
    }
}

// ── Node health check ───────────────────────────────────────────────────────

/// POST /api/nodes/:id/check — SSH into the node, update its status, return it.
async fn check_node(State(state): State<AppState>, Path(id): Path<String>) -> Response {
    let node = match sqlx::query_as::<_, Node>("SELECT id, name, ip, port, ssh_user, COALESCE(ssh_pass, '') AS ssh_pass, status, CAST(created_at AS TEXT) AS created_at FROM nodes WHERE id = $1")
        .bind(&id)
        .fetch_optional(&state.db)
        .await
    {
        Ok(Some(n)) => n,
        Ok(None) => return (StatusCode::NOT_FOUND, Json(serde_json::json!({ "error": "Нода не найдена" }))).into_response(),
        Err(e) => return (StatusCode::INTERNAL_SERVER_ERROR, Json(serde_json::json!({ "error": format!("БД: {e}") }))).into_response(),
    };

    let status = match run_node_command(&node, "echo OK").await {
        Ok(_) => "online".to_string(),
        Err(e) => {
            tracing::warn!("Node {} check failed: {e}", node.name);
            "offline".to_string()
        }
    };
    let _ = sqlx::query("UPDATE nodes SET status = $1 WHERE id = $2")
        .bind(&status)
        .bind(&id)
        .execute(&state.db)
        .await;

    Json(serde_json::json!({ "id": id, "status": status })).into_response()
}

// ── SSH command runner ──────────────────────────────────────────────────────

/// Run `command` on the node via SSH and return its stdout.
/// Shared by the health check, monitoring and user deletion.
async fn run_node_command(node: &Node, command: &str) -> Result<String, String> {
    let ssh_pass = node
        .ssh_pass
        .as_deref()
        .ok_or_else(|| "у ноды не задан SSH пароль".to_string())?;

    let fut = async {
        let config = Arc::new(russh::client::Config::default());
        let mut session = russh::client::connect(
            config,
            (node.ip.as_str(), u16::try_from(node.port).unwrap_or(22)),
            NodeSshHandler,
        )
        .await
        .map_err(|e| format!("SSH connect: {e}"))?;

        let authed = session
            .authenticate_password(&node.ssh_user, ssh_pass)
            .await
            .map_err(|e| format!("SSH auth: {e}"))?;
        if !authed {
            return Err("SSH auth: неверный логин/пароль ноды".to_string());
        }

        let mut channel = session
            .channel_open_session()
            .await
            .map_err(|e| format!("SSH channel: {e}"))?;
        channel
            .exec(true, command)
            .await
            .map_err(|e| format!("SSH exec: {e}"))?;

        let mut output = String::new();
        while let Some(msg) = channel.wait().await {
            if let russh::ChannelMsg::Data { ref data } = msg {
                output.push_str(&String::from_utf8_lossy(data));
            }
        }
        let _ = session
            .disconnect(russh::Disconnect::ByApplication, "done", "en")
            .await;
        Ok(output)
    };

    tokio::time::timeout(SSH_PROVISION_TIMEOUT, fut)
        .await
        .map_err(|_| "таймаут SSH".to_string())?
}

// ── Real monitoring over SSH ────────────────────────────────────────────────

#[derive(Serialize)]
struct NodeMonitor {
    id: String,
    cpu_percent: f64,
    ram_mb: f64,
    ram_total_mb: f64,
    tx_bytes: f64,
    rx_bytes: f64,
    online: bool,
}

/// GET /api/monitoring — real CPU/RAM/traffic per node via SSH (one probe per
/// node, all in parallel; nodes that fail SSH report online=false).
async fn get_monitoring(State(state): State<AppState>) -> Response {
    let nodes = sqlx::query_as::<_, Node>("SELECT id, name, ip, port, ssh_user, COALESCE(ssh_pass, '') AS ssh_pass, status, CAST(created_at AS TEXT) AS created_at FROM nodes")
        .fetch_all(&state.db)
        .await
        .unwrap_or_default();

    // CPU%, MemAvailable MB, MemTotal MB, rx bytes, tx bytes — one line, space-separated
    let cmd = "echo \"$((100 - $(top -bn1 | grep 'Cpu(s)' | awk '{print int($8)}'))) $(grep MemAvailable /proc/meminfo | awk '{print int($2/1024)}') $(grep MemTotal /proc/meminfo | awk '{print int($2/1024)}') $(cat /proc/net/dev | awk '/:/{sub(/:/,\"\"); if ($1 != \"lo\") {rx+=$2; tx+=$10}} END {print rx, tx}')\"";

    let probes: Vec<_> = nodes
        .iter()
        .map(|node| {
            let owned: Node = Node {
                id: node.id.clone(),
                name: node.name.clone(),
                ip: node.ip.clone(),
                port: node.port,
                ssh_user: node.ssh_user.clone(),
                ssh_pass: node.ssh_pass.clone(),
                status: node.status.clone(),
                created_at: node.created_at.clone(),
            };
            let cmd = cmd.to_string();
            tokio::spawn(async move {
                match run_node_command(&owned, &cmd).await {
                    Ok(out) => {
                        let mut it = out.split_whitespace().map(|v| v.parse::<f64>().unwrap_or(0.0));
                        (
                            owned.id,
                            NodeMonitor {
                                id: String::new(),
                                cpu_percent: it.next().unwrap_or(0.0),
                                ram_mb: it.next().unwrap_or(0.0),
                                ram_total_mb: it.next().unwrap_or(0.0),
                                rx_bytes: it.next().unwrap_or(0.0),
                                tx_bytes: it.next().unwrap_or(0.0),
                                online: true,
                            },
                        )
                    }
                    Err(_) => (
                        owned.id,
                        NodeMonitor {
                            id: String::new(), cpu_percent: 0.0, ram_mb: 0.0, ram_total_mb: 0.0,
                            tx_bytes: 0.0, rx_bytes: 0.0, online: false,
                        },
                    ),
                }
            })
        })
        .collect();

    let mut stats = Vec::new();
    for probe in probes {
        if let Ok((id, mut m)) = probe.await {
            m.id = id;
            stats.push(m);
        }
    }
    Json(stats).into_response()
}

// ── Update check (GitHub Releases) ──────────────────────────────────────────

#[derive(Serialize)]
struct UpdateInfo {
    current_version: String,
    latest_version: String,
    update_available: bool,
    release_url: String,
    release_notes: String,
}

fn update_info_unavailable(reason: String) -> UpdateInfo {
    UpdateInfo {
        current_version: option_env!("UPDATE_COMMIT").unwrap_or("unknown").to_string(),
        latest_version: String::new(),
        update_available: false,
        release_url: String::new(),
        release_notes: reason,
    }
}

/// GET /api/update — compare the running build's commit against the tip of
/// main on GitHub. The update flow ships commits (no releases), so the commit
/// SHA is the version: the build stamps it via the UPDATE_COMMIT env var
/// (install.sh / update.sh pass it to docker build), and any change on main
/// means an update is available.
async fn get_update_info() -> Response {
    let current = option_env!("UPDATE_COMMIT").unwrap_or("unknown").to_string();
    let info = match reqwest::Client::builder()
        .timeout(Duration::from_secs(10))
        .build()
    {
        Ok(client) => match client
            .get(format!("https://api.github.com/repos/{UPDATE_REPO}/commits/main"))
            .header("User-Agent", "forgefox-provider")
            .header("Accept", "application/vnd.github+json")
            .send()
            .await
        {
            Ok(resp) if resp.status().is_success() => match resp.json::<serde_json::Value>().await {
                Ok(json) => {
                    let latest = json["sha"].as_str().unwrap_or("").to_string();
                    let short = if latest.len() >= 7 { latest[..7].to_string() } else { latest.clone() };
                    let url = json["html_url"].as_str().unwrap_or_default().to_string();
                    let message = json["commit"]["message"].as_str().unwrap_or_default().lines().next().unwrap_or("").to_string();
                    // A build stamped with its commit is stale whenever the
                    // tip of main differs. "unknown" means the image was
                    // built without --build-arg (every install made before
                    // that existed) — offer the update so those panels can
                    // bootstrap themselves onto the stamped build.
                    let update_available = !latest.is_empty()
                        && (current == "unknown" || !latest.starts_with(current.as_str()));
                    UpdateInfo {
                        current_version: current,
                        latest_version: short,
                        update_available,
                        release_url: url,
                        release_notes: message,
                    }
                }
                Err(e) => update_info_unavailable(format!("не удалось разобрать ответ: {e}")),
            },
            Ok(resp) => update_info_unavailable(format!("GitHub ответил HTTP {}", resp.status())),
            Err(e) => update_info_unavailable(format!("нет соединения с GitHub: {e}")),
        },
        Err(_) => update_info_unavailable("HTTP-клиент недоступен".into()),
    };
    Json(info).into_response()
}

// ── Self-update (POST /api/update/run) ──────────────────────────────────────
//
// The panel container runs with /var/run/docker.sock mounted (see install.sh
// docker-compose) and /opt/forgefox-provider/update.sh bind-mounted from the
// host. The endpoint spawns that script detached (nohup, output to a log file
// on the host) and returns immediately — the script re-pulls/rebuilds the
// image and recreates this container, which severs the connection. The UI
// polls /api/update until the new version answers.

async fn run_self_update() -> Response {
    let script = "/app/update.sh";
    if !std::path::Path::new(script).exists() {
        return (
            StatusCode::CONFLICT,
            Json(serde_json::json!({
                "error": "Скрипт обновления не найден. Панель установлена старой версией install.sh — обновите вручную: curl -Ls https://raw.githubusercontent.com/KiAtsushi-Git/Forge-Fox-VPN-Self-Host-Provider/main/install.sh | bash"
            })),
        ).into_response();
    }

    // Detached: this process (and its HTTP response) may be killed mid-update.
    let res = tokio::process::Command::new("bash")
        .arg("-c")
        .arg(format!("nohup bash {script} > /opt/forgefox-provider/update.log 2>&1 &"))
        .spawn();

    match res {
        Ok(_) => Json(serde_json::json!({ "status": "updating" })).into_response(),
        Err(e) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(serde_json::json!({ "error": format!("не удалось запустить обновление: {e}") })),
        ).into_response(),
    }
}

// ── Audit log (real) ────────────────────────────────────────────────────────

async fn audit(state: &AppState, action: &str) {
    let time = chrono::Local::now().format("%Y-%m-%d %H:%M:%S").to_string();
    let res = sqlx::query("INSERT INTO audit_log (id, time, action) VALUES ($1, $2, $3)")
        .bind(uuid::Uuid::new_v4().to_string())
        .bind(&time)
        .bind(action)
        .execute(&state.db)
        .await;
    if let Err(e) = res {
        tracing::warn!("audit insert failed: {e}");
    }
}

async fn get_logs(State(state): State<AppState>) -> Response {
    let rows = sqlx::query_as::<_, (String, String)>(
        "SELECT CAST(time AS TEXT) AS time, action FROM audit_log ORDER BY CAST(time AS TEXT) DESC",
    )
    .fetch_all(&state.db)
    .await
    .unwrap_or_default();
    Json(rows).into_response()
}

// ── Startup ──────────────────────────────────────────────────────────────────

/// Sync admin credentials from the environment (set by install.sh via
/// `--user` / `--pass`) into the admins table, so the credentials used at
/// install time always work for panel login.
async fn seed_admin(db: &AnyPool, username: &str, password: &str) {
    let hash = bcrypt::hash(password, bcrypt::DEFAULT_COST)
        .expect("Failed to hash admin password");

    sqlx::query(
        "INSERT INTO admins (id, username, password_hash) VALUES ($1, $2, $3) \
         ON CONFLICT(username) DO UPDATE SET password_hash = excluded.password_hash",
    )
    .bind(uuid::Uuid::new_v4().to_string())
    .bind(username)
    .bind(&hash)
    .execute(db)
    .await
    .expect("Failed to seed admin user");

    tracing::info!("Admin user '{}' is ready", username);
}

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt::init();
    tracing::info!("Starting ForgeFox VPN Provider...");

    // Connect to the database. DATABASE_URL decides the backend:
    // "postgres://..." → PostgreSQL, anything else ("sqlite://..." or empty) → SQLite.
    // install.sh passes the URL matching the --db choice (postgres|sqlite).
    let db_url = std::env::var("DATABASE_URL").unwrap_or_else(|_| "sqlite://forgefox.db?mode=rwc".to_string());
    sqlx::any::install_default_drivers();
    let pool = AnyPoolOptions::new()
        .max_connections(5)
        .connect(&db_url)
        .await
        .expect("Failed to connect to the database");

    // Run migrations
    sqlx::migrate!("./migrations")
        .run(&pool)
        .await
        .expect("Failed to run DB migrations");

    // Admin credentials come from install.sh / docker-compose env
    let admin_user = std::env::var("ADMIN_USER").unwrap_or_else(|_| "admin".to_string());
    let admin_pass = std::env::var("ADMIN_PASS").unwrap_or_else(|_| "admin".to_string());
    seed_admin(&pool, &admin_user, &admin_pass).await;

    let state = AppState {
        db: pool,
        sessions: Arc::new(tokio::sync::Mutex::new(HashMap::new())),
    };

    // Build the router
    let api_routes = Router::new()
        .route("/dashboard", get(get_dashboard))
        .route("/nodes", get(get_nodes).post(add_node))
        .route("/nodes/:id", axum::routing::put(update_node).delete(delete_node))
        .route("/nodes/:id/check", post(check_node))
        .route("/clients", get(get_clients).post(add_client))
        .route("/clients/:id", delete(delete_client))
        .route("/monitoring", get(get_monitoring))
        .route("/logs", get(get_logs))
        .route("/update", get(get_update_info))
        .route("/update/run", post(run_self_update))
        .route_layer(middleware::from_fn_with_state(
            state.clone(),
            auth_middleware,
        ));

    let app = Router::new()
        // Public: login and client subscriptions
        .route("/api/login", post(login))
        .nest("/api", api_routes)
        .route("/sub/:id", get(get_subscription))
        .with_state(state)
        // Serve frontend files
        .nest_service("/", ServeDir::new("public").not_found_service(ServeFile::new("public/index.html")));

    let addr = SocketAddr::from(([0, 0, 0, 0], 8080));
    tracing::info!("Web UI listening on http://{}", addr);

    let listener = tokio::net::TcpListener::bind(&addr).await.unwrap();
    axum::serve(listener, app).await.unwrap();
}
