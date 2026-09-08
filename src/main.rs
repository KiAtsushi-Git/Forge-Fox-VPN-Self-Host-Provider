mod models;

use axum::{
    extract::{Path, Request, State},
    http::{header, StatusCode},
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{get, post},
    Router, Json,
};
use serde::{Deserialize, Serialize};
use sqlx::{sqlite::SqlitePoolOptions, SqlitePool};
use std::collections::HashMap;
use std::net::SocketAddr;
use std::sync::Arc;
use std::time::{Duration, Instant};
use tower_http::services::{ServeDir, ServeFile};
use models::{Node, Client};

const SESSION_TTL: Duration = Duration::from_secs(24 * 60 * 60);

#[derive(Clone)]
struct AppState {
    db: SqlitePool,
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
    let nodes = sqlx::query_as::<_, Node>("SELECT * FROM nodes")
        .fetch_all(&state.db)
        .await
        .unwrap_or_default();
    Json(nodes)
}

async fn add_node(State(state): State<AppState>, Json(payload): Json<Node>) -> Json<Node> {
    // In real app, generate UUID if missing, and SSH into node to deploy bridge
    let id = uuid::Uuid::new_v4().to_string();
    sqlx::query("INSERT INTO nodes (id, name, ip, port, ssh_user, status) VALUES (?, ?, ?, ?, ?, ?)")
        .bind(&id)
        .bind(&payload.name)
        .bind(&payload.ip)
        .bind(payload.port)
        .bind(&payload.ssh_user)
        .bind("online")
        .execute(&state.db)
        .await
        .expect("Failed to insert node");

    let new_node = sqlx::query_as::<_, Node>("SELECT * FROM nodes WHERE id = ?")
        .bind(&id)
        .fetch_one(&state.db)
        .await
        .unwrap();

    Json(new_node)
}

async fn get_clients(State(state): State<AppState>) -> Json<Vec<Client>> {
    let clients = sqlx::query_as::<_, Client>("SELECT * FROM clients")
        .fetch_all(&state.db)
        .await
        .unwrap_or_default();
    Json(clients)
}

async fn add_client(State(state): State<AppState>, Json(payload): Json<Client>) -> Json<Client> {
    let id = uuid::Uuid::new_v4().to_string();
    sqlx::query("INSERT INTO clients (id, username, node_id, expiry, limit_gb, used_bytes) VALUES (?, ?, ?, ?, ?, 0)")
        .bind(&id)
        .bind(&payload.username)
        .bind(&payload.node_id)
        .bind(payload.expiry)
        .bind(payload.limit_gb)
        .execute(&state.db)
        .await
        .expect("Failed to insert client");

    let new_client = sqlx::query_as::<_, Client>("SELECT * FROM clients WHERE id = ?")
        .bind(&id)
        .fetch_one(&state.db)
        .await
        .unwrap();

    Json(new_client)
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
        sqlx::query_as("SELECT username, password_hash FROM admins WHERE username = ?")
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
    let client = sqlx::query_as::<_, Client>("SELECT * FROM clients WHERE id = ?")
        .bind(&id)
        .fetch_optional(&state.db)
        .await
        .unwrap_or(None);

    let client = match client {
        Some(c) => c,
        None => return (StatusCode::NOT_FOUND, "Client not found").into_response(),
    };

    let node = sqlx::query_as::<_, Node>("SELECT * FROM nodes WHERE id = ?")
        .bind(&client.node_id)
        .fetch_optional(&state.db)
        .await
        .unwrap_or(None);

    let node = match node {
        Some(n) => n,
        None => return (StatusCode::NOT_FOUND, "Node not found").into_response(),
    };

    // Note: in a real app, the password would be generated on the Node during creation and stored.
    // For now, we return a mock password or the client ID.
    let name = format!("{} ({})", node.name, client.username);
    let body = format!(
        "ssh://{}:{}@{}:{}#{}\n",
        client.username,
        "generated_password_here",
        node.ip,
        node.port,
        encode_fragment(&name)
    );

    (
        [(header::CONTENT_TYPE, "text/plain; charset=utf-8")],
        body,
    )
        .into_response()
}

// ── Monitoring / Logs (mock) ────────────────────────────────────────────────

#[derive(Serialize)]
struct NodeMonitor {
    id: String,
    cpu_percent: f64,
    ram_mb: i64,
    tx_kbps: i64,
    rx_kbps: i64,
}

async fn get_monitoring(State(state): State<AppState>) -> Json<Vec<NodeMonitor>> {
    let nodes = sqlx::query_as::<_, Node>("SELECT * FROM nodes")
        .fetch_all(&state.db)
        .await
        .unwrap_or_default();

    let mut stats = Vec::new();
    // Generate random stats for UI
    for node in nodes {
        stats.push(NodeMonitor {
            id: node.id,
            cpu_percent: rand::random::<f64>() * 100.0,
            ram_mb: rand::random::<i64>() % 4096,
            tx_kbps: rand::random::<i64>() % 10000,
            rx_kbps: rand::random::<i64>() % 10000,
        });
    }
    Json(stats)
}

#[derive(Serialize)]
struct AuditLog {
    time: String,
    action: String,
    user: String,
}

async fn get_logs() -> Json<Vec<AuditLog>> {
    Json(vec![
        AuditLog { time: "10:45:01".into(), action: "Успешный вход в панель".into(), user: "admin".into() },
        AuditLog { time: "10:42:12".into(), action: "Создан пользователь ivan".into(), user: "admin".into() },
        AuditLog { time: "10:30:00".into(), action: "Добавлена новая нода Germany-1".into(), user: "admin".into() },
    ])
}

// ── Startup ──────────────────────────────────────────────────────────────────

/// Sync admin credentials from the environment (set by install.sh via
/// `--user` / `--pass`) into the admins table, so the credentials used at
/// install time always work for panel login.
async fn seed_admin(db: &SqlitePool, username: &str, password: &str) {
    let hash = bcrypt::hash(password, bcrypt::DEFAULT_COST)
        .expect("Failed to hash admin password");

    sqlx::query(
        "INSERT INTO admins (id, username, password_hash) VALUES (?, ?, ?) \
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

    // Connect to SQLite
    let db_url = std::env::var("DATABASE_URL").unwrap_or_else(|_| "sqlite://forgefox.db?mode=rwc".to_string());
    let pool = SqlitePoolOptions::new()
        .max_connections(5)
        .connect(&db_url)
        .await
        .expect("Failed to connect to SQLite");

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
        .route("/clients", get(get_clients).post(add_client))
        .route("/monitoring", get(get_monitoring))
        .route("/logs", get(get_logs))
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
