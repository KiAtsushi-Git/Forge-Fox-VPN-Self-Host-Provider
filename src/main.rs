mod models;

use axum::{
    extract::State,
    routing::{get, post},
    Router,
    Json,
};
use serde::{Deserialize, Serialize};
use sqlx::{sqlite::SqlitePoolOptions, SqlitePool};
use std::net::SocketAddr;
use tower_http::services::{ServeDir, ServeFile};
use models::{Node, Client};

#[derive(Clone)]
struct AppState {
    db: SqlitePool,
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

#[derive(Serialize)]
struct SubServer {
    name: String,
    host: String,
    port: i64,
    user: String,
    pass: String,
}

#[derive(Serialize)]
struct SubResponse {
    servers: Vec<SubServer>,
}

async fn get_subscription(
    State(state): State<AppState>,
    axum::extract::Path(id): axum::extract::Path<String>,
) -> Json<SubResponse> {
    // 1. Find client
    let client = sqlx::query_as::<_, Client>("SELECT * FROM clients WHERE id = ?")
        .bind(&id)
        .fetch_optional(&state.db)
        .await
        .unwrap();

    if let Some(c) = client {
        // 2. Find node
        let node = sqlx::query_as::<_, Node>("SELECT * FROM nodes WHERE id = ?")
            .bind(&c.node_id)
            .fetch_optional(&state.db)
            .await
            .unwrap();

        if let Some(n) = node {
            // Note: in a real app, the password would be generated on the Node during creation and stored.
            // For now, we return a mock password or the client ID.
            return Json(SubResponse {
                servers: vec![SubServer {
                    name: format!("{} ({})", n.name, c.username),
                    host: n.ip,
                    port: n.port,
                    user: c.username,
                    pass: "generated_password_here".to_string(),
                }],
            });
        }
    }

    Json(SubResponse { servers: vec![] })
}

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

    let state = AppState { db: pool };

    // Build the router
    let api_routes = Router::new()
        .route("/dashboard", get(get_dashboard))
        .route("/nodes", get(get_nodes).post(add_node))
        .route("/clients", get(get_clients).post(add_client))
        .route("/monitoring", get(get_monitoring))
        .route("/logs", get(get_logs));

    let app = Router::new()
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
