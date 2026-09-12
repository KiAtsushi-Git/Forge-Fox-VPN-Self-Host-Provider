"""Pydantic request schemas (responses are built by hand to keep the exact
JSON shapes the frontend already consumes)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str
    password: str


class NewNode(BaseModel):
    name: str
    ip: str
    port: int | None = None
    ssh_user: str | None = None
    ssh_pass: str | None = None


class NodeUpdate(BaseModel):
    name: str | None = None
    ip: str | None = None
    port: int | None = None
    ssh_user: str | None = None
    ssh_pass: str | None = None


class NewClient(BaseModel):
    username: str
    node_id: str | None = None       # legacy single-node field
    node_ids: list[str] | None = None
    expiry: str | None = None        # "YYYY-MM-DDTHH:MM" from datetime-local
    limit_gb: int | None = None


class BulkNewClients(BaseModel):
    usernames: list[str] = Field(min_length=1)
    node_ids: list[str] = Field(min_length=1)
    expiry: str | None = None
    limit_gb: int | None = None


class ClientUpdate(BaseModel):
    expiry: str | None = None
    limit_gb: int | None = None
    node_ids: list[str] | None = None
    status: str | None = None


class SettingsUpdate(BaseModel):
    telegram_bot_token: str | None = None
    telegram_admin_chat_id: str | None = None
    notify_expiry: bool | None = None
    notify_limit: bool | None = None
    notify_node_offline: bool | None = None
    node_poll_interval: int | None = None
    traffic_poll_interval: int | None = None
    panel_url: str | None = None


class PasswordChange(BaseModel):
    old_password: str
    new_password: str


class ImportClients(BaseModel):
    clients: list[dict]
