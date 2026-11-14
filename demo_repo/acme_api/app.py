"""HTTP API for users."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Response, status
from pydantic import BaseModel, Field

from acme_api import db

EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"

app = FastAPI(title="acme-api")


class UserIn(BaseModel):
    email: str = Field(pattern=EMAIL_PATTERN)
    name: str = Field(min_length=1)


class Rename(BaseModel):
    name: str = Field(min_length=1)


class User(BaseModel):
    id: int
    email: str
    name: str


def _row_to_user(row: tuple) -> User:
    return User(id=row[0], email=row[1], name=row[2])


def _get(conn, user_id: int) -> User:
    row = db.execute(conn, "SELECT id, email, name FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"user {user_id} not found")
    return _row_to_user(row)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/users")
def list_users() -> list[User]:
    with db.connection() as conn:
        rows = db.execute(conn, "SELECT id, email, name FROM users ORDER BY id").fetchall()
    return [_row_to_user(row) for row in rows]


@app.post("/users", status_code=status.HTTP_201_CREATED)
def create_user(user: UserIn) -> User:
    with db.connection() as conn:
        try:
            db.execute(conn, "INSERT INTO users (email, name) VALUES (?, ?)", (user.email, user.name))
        except db.INTEGRITY_ERRORS:
            raise HTTPException(status.HTTP_409_CONFLICT, f"{user.email} already exists") from None
        row = db.execute(conn, "SELECT id, email, name FROM users WHERE email = ?", (user.email,)).fetchone()
    return _row_to_user(row)


@app.get("/users/{user_id}")
def get_user(user_id: int) -> User:
    with db.connection() as conn:
        return _get(conn, user_id)


@app.patch("/users/{user_id}")
def rename_user(user_id: int, rename: Rename) -> User:
    with db.connection() as conn:
        _get(conn, user_id)
        db.execute(conn, "UPDATE users SET name = ? WHERE id = ?", (rename.name, user_id))
        return _get(conn, user_id)


@app.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(user_id: int) -> Response:
    with db.connection() as conn:
        _get(conn, user_id)
        db.execute(conn, "DELETE FROM users WHERE id = ?", (user_id,))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
