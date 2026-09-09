use serde::{Serialize, Deserialize};
use sqlx::FromRow;

// Date/time fields are Strings, not chrono::NaiveDateTime: sqlx's `Any` driver
// (needed to support both SQLite and PostgreSQL from one build) has no
// chrono date/time support. SQLite returns the stored "YYYY-MM-DD HH:MM:SS"
// text as-is; PostgreSQL returns the same shape for TIMESTAMP columns.
#[derive(Debug, Serialize, Deserialize, FromRow)]
pub struct Node {
    pub id: String,
    pub name: String,
    pub ip: String,
    pub port: i64,
    pub ssh_user: String,
    #[serde(skip_serializing)]
    pub ssh_pass: Option<String>,
    pub status: Option<String>,
    pub created_at: Option<String>,
}

#[derive(Debug, Serialize, Deserialize, FromRow)]
pub struct Client {
    pub id: String,
    pub username: String,
    pub node_id: String,
    #[serde(skip_serializing)]
    pub password: Option<String>,
    pub expiry: Option<String>,
    pub limit_gb: Option<i64>,
    pub used_bytes: Option<i64>,
    pub created_at: Option<String>,
}
