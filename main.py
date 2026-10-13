import json
import os
import random
import string
import time
import uuid

import httpx
import pymysql
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.responses import RedirectResponse

DB_HOST = os.environ.get("DB_HOST", "127.0.0.1")
DB_PORT = int(os.environ.get("DB_PORT", "3306"))
DB_USER = os.environ.get("DB_USER", "root")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")
DB_NAME = os.environ.get("DB_NAME", "a35p9hbx")

# 大模型 API 配置（兼容 OpenAI / DeepSeek / 豆包 / Kimi 等规范端点）
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-chat")

UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = FastAPI(title="你那边 · 异国牵挂台 API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_conn():
    return pymysql.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        charset="utf8mb4",
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


def gen_room_id():
    chars = string.ascii_uppercase + string.digits
    chars = chars.replace("0", "").replace("O", "").replace("1", "").replace("I", "")
    return "".join(random.choices(chars, k=6))


class CreateRoomBody(BaseModel):
    cfg: dict


class UpdateRoomBody(BaseModel):
    cfg: dict
    version: int


class AICareRequest(BaseModel):
    my_city: str
    their_city: str
    time_diff_desc: str
    their_weather: str
    window_desc: str


@app.post("/api/upload")
async def upload_image(file: UploadFile = File(...)):
    ext = ""
    if file.filename and "." in file.filename:
        ext = "." + file.filename.rsplit(".", 1)[-1].lower()
    if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        ext = ".png"
    name = uuid.uuid4().hex + ext
    dest = os.path.join(UPLOAD_DIR, name)
    content = await file.read()
    if len(content) > 8 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="file too large")
    with open(dest, "wb") as f:
        f.write(content)
    return {"url": f"/static/uploads/{name}"}


@app.get("/api/room/{room_id}")
def get_room(room_id: str):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, cfg, version, updated_at FROM rooms WHERE id=%s",
                (room_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="room not found")
            return {
                "id": row["id"],
                "cfg": json.loads(row["cfg"]),
                "version": row["version"],
                "updated_at": row["updated_at"],
            }
    finally:
        conn.close()


@app.post("/api/room")
def create_room(body: CreateRoomBody):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            for _ in range(10):
                room_id = gen_room_id()
                cur.execute("SELECT id FROM rooms WHERE id=%s", (room_id,))
                if not cur.fetchone():
                    break
            else:
                raise HTTPException(status_code=500, detail="failed to allocate room id")
            now = int(time.time() * 1000)
            cur.execute(
                "INSERT INTO rooms (id, cfg, version, updated_at, created_at) VALUES (%s,%s,1,%s,%s)",
                (room_id, json.dumps(body.cfg, ensure_ascii=False), now, now),
            )
            return {"id": room_id, "version": 1, "updated_at": now}
    finally:
        conn.close()


@app.put("/api/room/{room_id}")
def update_room(room_id: str, body: UpdateRoomBody):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT version FROM rooms WHERE id=%s", (room_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="room not found")
            if row["version"] != body.version:
                cur.execute(
                    "SELECT id, cfg, version, updated_at FROM rooms WHERE id=%s",
                    (room_id,),
                )
                cur_row = cur.fetchone()
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "version conflict",
                        "cfg": json.loads(cur_row["cfg"]),
                        "version": cur_row["version"],
                        "updated_at": cur_row["updated_at"],
                    },
                )
            now = int(time.time() * 1000)
            new_version = body.version + 1
            cur.execute(
                "UPDATE rooms SET cfg=%s, version=%s, updated_at=%s WHERE id=%s",
                (json.dumps(body.cfg, ensure_ascii=False), new_version, now, room_id),
            )
            return {"id": room_id, "version": new_version, "updated_at": now}
    finally:
        conn.close()


@app.post("/api/ai/care")
async def generate_ai_care(req: AICareRequest):
    """
    AI 跨时区生活管家：将两地时差、对方实时气象与未来空闲重叠窗口注入 Prompt Context，
    生成一段兼具温情提醒与沟通破冰建议的回答。
    """
    system_prompt = (
        "你是一个专为异地/异国情侣和亲友设计的情感连接 AI 助手。你的核心能力是根据双方当前的时空动态，"
        "生成简短、自然、温情且具有实际行动建议的【跨时区叮嘱】与【破冰聊天灵感】。\n"
        "要求：\n"
        "1. 语气真诚温暖、生活化，切忌机械的说教和套话。\n"
        "2. 控制在 120 字以内。\n"
        "3. 紧扣传入的对方天气、当前作息与下一次重叠时间。"
    )

    user_prompt = (
        f"【当前时空上下文】\n"
        f"- 我在城市：{req.my_city}\n"
        f"- TA在城市：{req.their_city}\n"
        f"- 双方作息/睡眠状态：{req.time_diff_desc}\n"
        f"- TA所在天气状况：{req.their_weather}\n"
        f"- 下一次双方都清醒的通话窗口：{req.window_desc}\n\n"
        f"请为我生成一段此刻适合发给 TA 或默默了解的关怀叮嘱与话题灵感："
    )

    # 兜底生成逻辑（无 Key 或网络异常时保障演示）
    fallback_text = (
        f"此时{req.their_city}正值{req.their_weather}，出门或休息记得叮嘱TA多照顾自己。"
        f"下一次大家都清醒是在 {req.window_desc}，到时候不妨视频聊聊今天彼此生活里发生的有趣小事～"
    )

    if not LLM_API_KEY:
        return {"reply": fallback_text, "source": "local_rule_engine"}

    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            resp = await client.post(
                f"{LLM_BASE_URL.rstrip('/')}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {LLM_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": LLM_MODEL,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.7,
                    "max_tokens": 300,
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                reply = data["choices"][0]["message"]["content"].strip()
                return {"reply": reply, "source": "llm_agent"}
            else:
                return {"reply": fallback_text, "source": "fallback"}
    except Exception:
        return {"reply": fallback_text, "source": "fallback"}


@app.get("/")
async def root():
    return RedirectResponse(url="/static/", status_code=302)


app.mount("/static", StaticFiles(directory="static", html=True), name="static")
