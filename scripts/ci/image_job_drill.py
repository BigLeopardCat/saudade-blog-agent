"""Exercise actual image completion HTTP authorization and the booted widget in CI."""

import base64
import hashlib
import json
import subprocess
import time
import uuid

import httpx
import jwt
import psycopg


def run_image_drill(agent, work, out, dsn, session_key, provider, common):
    assert "isolated" in dsn or "chat_stack" in dsn or "stack_" in dsn
    pixels = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/"
        "x8AAwMCAO+jC2kAAAAASUVORK5CYII=")
    request = {"provider": "https://synthetic-image.invalid/v1", "model": "fixture",
               "prompt": "Synthetic completed image", "size": "1536x1024",
               "quality": "high", "references": []}

    def seed(role):
        with psycopg.connect(dsn, autocommit=True) as db:
            uid = db.execute('INSERT INTO "user"(username,password,role) '
                "VALUES(%s,'fixture-unused',%s) RETURNING id",
                ("image_stack_" + uuid.uuid4().hex, role)).fetchone()[0]
            cid = db.execute("INSERT INTO conversation(user_id,title) VALUES(%s,'Image task') "
                             "RETURNING id", (uid,)).fetchone()[0]
        return uid, cid

    def token(uid, role):
        return jwt.encode({"sub": uid, "role": role, "ver": 0, "aud": "blog-session",
                           "exp": int(time.time()) + 300}, session_key, algorithm="HS256")

    def new_job(uid, cid):
        ident = str(uuid.uuid4())
        with psycopg.connect(dsn, autocommit=True) as db:
            db.execute("INSERT INTO image_generation(id,owner_id,conversation_id,request,"
                "request_hash,status,origin,approved_at) VALUES(%s::uuid,%s,%s,%s::jsonb,%s,"
                "'generating','agent',now())",
                (ident, uid, cid, json.dumps(request), "a" * 64))
        return ident

    def version(ident):
        with psycopg.connect(dsn) as db:
            return db.execute("SELECT encode(sha256(convert_to("
                "jsonb_build_array(status,output_hash,image_id,error)::text,'UTF8')),'hex') "
                "FROM image_generation WHERE id=%s::uuid", (ident,)).fetchone()[0]

    origin = "http://127.0.0.1:18080"
    with httpx.Client(trust_env=False, timeout=15) as client:
        for role in ("user", "zako"):
            uid, cid = seed(role)
            ident = new_job(uid, cid)
            response = client.post(origin + "/api/chat/stream",
                headers={"Authorization": "Bearer " + token(uid, role)}, json={
                    "message": "", "conversation_id": cid,
                    "image_event": {"job_id": ident, "version": version(ident)}})
            assert response.status_code == 403, (role, response.status_code)
    uid, cid = seed("superadmin")
    current = {"id": new_job(uid, cid)}

    def control(value):
        if value["action"] == "new":
            current["id"] = new_job(uid, cid)
        elif value["action"] == "complete":
            with psycopg.connect(dsn, autocommit=True) as db:
                db.execute("UPDATE image_generation SET status='review',output=%s,output_hash=%s "
                           "WHERE id=%s::uuid AND owner_id=%s",
                           (pixels, hashlib.sha256(pixels).hexdigest(), current["id"], uid))
        else:
            raise ValueError("Unknown fixture transition")
        return {"id": current["id"]}

    provider.image_control = control
    fixture = work / "image-fixture.json"
    fixture.write_text(json.dumps({"token": token(uid, "superadmin"), "uid": uid, "cid": cid,
        "id": current["id"], "origin": origin,
        "provider": f"http://127.0.0.1:{provider.server_port}"}))
    fixture.chmod(0o600)
    subprocess.run(["node", "frontend/tests/image-stack.browser.mjs"], cwd=agent,
        env={**common, "IMAGE_STACK_FIXTURE": str(fixture),
             "CHAT_STACK_ARTIFACT_DIR": str(out)}, check=True, timeout=110)
    with psycopg.connect(dsn, autocommit=True) as db:
        count = db.execute("SELECT count(*) FROM chat_history WHERE user_id=%s AND role='user'",
                           (uid,)).fetchone()[0]
        assert count == 1, "Completion reporting fabricated a human message"
        rows = db.execute("SELECT status,image_id,reported_version IS NOT NULL "
                          "FROM image_generation WHERE owner_id=%s ORDER BY created_at",
                          (uid,)).fetchall()
        assert rows[0][0] == "accepted" and rows[0][1] and rows[0][2]
        assert rows[1][0] == "review" and rows[1][2]
        db.execute('UPDATE "user" SET role=\'admin\' WHERE id=%s', (uid,))
    with httpx.Client(trust_env=False, timeout=15) as client:
        response = client.post(origin + "/api/chat/stream",
            headers={"Authorization": "Bearer " + token(uid, "admin")}, json={
                "message": "", "conversation_id": cid,
                "image_event": {"job_id": current["id"], "version": version(current["id"])}})
        assert response.status_code == 204, "Administrator cannot recover an already reported event"
    provider.image_control = None
    (out / "image-completion.json").write_text(json.dumps({"real_http_role_matrix": True,
        "background_report": True, "reopen_catchup": True, "local_acceptance": True,
        "fabricated_human_messages": 0, "external_image_calls": 0}))
