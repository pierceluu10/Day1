ADA = {"email": "ada@acme.test", "name": "Ada Lovelace"}
GRACE = {"email": "grace@acme.test", "name": "Grace Hopper"}


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_create_user_returns_it_with_an_id(client):
    response = client.post("/users", json=ADA)
    assert response.status_code == 201
    body = response.json()
    assert body["email"] == ADA["email"] and body["name"] == ADA["name"]
    assert isinstance(body["id"], int)


def test_created_user_is_persisted(client):
    user_id = client.post("/users", json=ADA).json()["id"]
    assert client.get(f"/users/{user_id}").json() == {"id": user_id, **ADA}


def test_duplicate_email_is_a_conflict(client):
    client.post("/users", json=ADA)
    response = client.post("/users", json={"email": ADA["email"], "name": "Someone Else"})
    assert response.status_code == 409
    assert len(client.get("/users").json()) == 1


def test_invalid_email_is_rejected(client):
    assert client.post("/users", json={"email": "not-an-email", "name": "X"}).status_code == 422
    assert client.get("/users").json() == []


def test_list_is_ordered_by_creation(client):
    first = client.post("/users", json=GRACE).json()["id"]
    second = client.post("/users", json=ADA).json()["id"]
    assert [user["id"] for user in client.get("/users").json()] == [first, second]


def test_missing_user_is_not_found(client):
    assert client.get("/users/999999").status_code == 404


def test_rename_changes_only_the_name(client):
    user_id = client.post("/users", json=ADA).json()["id"]
    response = client.patch(f"/users/{user_id}", json={"name": "Countess of Lovelace"})
    assert response.status_code == 200
    assert response.json() == {"id": user_id, "email": ADA["email"], "name": "Countess of Lovelace"}


def test_delete_removes_the_user(client):
    user_id = client.post("/users", json=ADA).json()["id"]
    assert client.delete(f"/users/{user_id}").status_code == 204
    assert client.get(f"/users/{user_id}").status_code == 404


def test_delete_missing_user_is_not_found(client):
    assert client.delete("/users/999999").status_code == 404
