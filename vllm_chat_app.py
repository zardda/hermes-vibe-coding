#!/usr/bin/env python3
"""MiniCPM-V-4.6 Chat - persistent sessions"""
import os, base64, sqlite3, datetime, hashlib
from pathlib import Path
import gradio as gr
from openai import OpenAI

VLLM_URL = os.environ.get("VLLM_URL", "http://localhost:8000/v1")
DB_PATH = os.environ.get("DB_PATH", str(Path.home() / ".vllm_chat.db"))
MAX_TOKENS = 2048

client = OpenAI(base_url=VLLM_URL, api_key="not-needed")
MODEL_ID = client.models.list().data[0].id
print(f"[chat] Model: {MODEL_ID}", flush=True)

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS convs (id TEXT PRIMARY KEY, title TEXT, created_at TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS msgs (id INTEGER PRIMARY KEY AUTOINCREMENT, conv_id TEXT, role TEXT, content TEXT, ts TEXT)")
    conn.commit(); conn.close()
init_db()

def new_conv():
    cid = hashlib.md5(str(datetime.datetime.now().timestamp()).encode()).hexdigest()[:12]
    now = datetime.datetime.now().isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO convs VALUES (?,?,?)", (cid, "New Chat", now))
    conn.commit(); conn.close()
    return cid

def save_msg(conv_id, role, content):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO msgs (conv_id,role,content,ts) VALUES (?,?,?,?)",
                 (conv_id, role, content, datetime.datetime.now().isoformat()))
    if role == "user":
        title = content[:40] + ("..." if len(content) > 40 else "")
        conn.execute("UPDATE convs SET title=? WHERE id=? AND title='New Chat'", (title, conv_id))
    conn.commit(); conn.close()

def load_msgs(conv_id):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT role,content FROM msgs WHERE conv_id=? ORDER BY id", (conv_id,)).fetchall()
    conn.close()
    return [{"role": r, "content": c} for r, c in rows]

def list_convs():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT id,title FROM convs ORDER BY created_at DESC").fetchall()
    conn.close()
    return [(t, i) for i, t in rows]

def del_conv(conv_id):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM msgs WHERE conv_id=?", (conv_id,))
    conn.execute("DELETE FROM convs WHERE id=?", (conv_id,))
    conn.commit(); conn.close()

def respond(msg, history, conv_id):
    history = list(history)
    text = ""
    files = []
    if isinstance(msg, str): text = msg
    elif isinstance(msg, dict):
        text = msg.get("text","") or ""
        files = msg.get("files",[]) or []
    else:
        text = getattr(msg,"text","") or ""
        files = list(getattr(msg,"files",[]) or [])

    if conv_id is None: conv_id = new_conv()
    save_msg(conv_id, "user", text or "(image)")

    openai_msgs = []
    for h in history:
        c = h["content"]
        openai_msgs.append({"role":h["role"], "content": c if isinstance(c,list) else str(c)})

    if files:
        fp = files[0] if isinstance(files[0],str) else getattr(files[0],"path",str(files[0]))
        with open(fp,"rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        ext = Path(fp).suffix.lower()
        m = {"jpg":"jpeg","jpeg":"jpeg","png":"png","gif":"gif","webp":"webp"}.get(ext.lstrip("."),"png")
        content = [{"type":"text","text":text or "Describe"},
                   {"type":"image_url","image_url":{"url":f"data:image/{m};base64,{b64}"}}]
        openai_msgs.append({"role":"user","content":content})
        history.append({"role":"user","content":text or "(image)"})
    else:
        openai_msgs.append({"role":"user","content":text or "hello"})
        history.append({"role":"user","content":text})

    try:
        resp = client.chat.completions.create(model=MODEL_ID, messages=openai_msgs, max_tokens=512, temperature=0.7)
        reply = resp.choices[0].message.content
    except Exception as e:
        reply = f"[Error: {e}]"

    save_msg(conv_id, "assistant", reply)
    history.append({"role":"assistant","content":reply})
    convs = list_convs()
    return "", history, conv_id, gr.Dropdown(choices=convs, value=conv_id)

def switch_conv(conv_id):
    if not conv_id: return [], None
    return load_msgs(conv_id), conv_id

def new_action():
    cid = new_conv()
    convs = list_convs()
    return [], cid, gr.Dropdown(choices=convs, value=cid)

def del_action(conv_id):
    if conv_id: del_conv(conv_id)
    convs = list_convs()
    return [], None, gr.Dropdown(choices=convs)

with gr.Blocks(title="MiniCPM-V Chat", fill_height=True) as app:
    current_conv = gr.State(None)
    with gr.Row(equal_height=False):
        with gr.Column(scale=1, min_width=200):
            gr.Markdown("### Sessions")
            conv_dd = gr.Dropdown(label="", choices=[], interactive=True)
            with gr.Row():
                new_btn = gr.Button("+ New", size="sm", variant="primary")
                del_btn = gr.Button("Delete", size="sm", variant="stop")
        with gr.Column(scale=4):
            chatbot = gr.Chatbot(height=500, render_markdown=True)
            msg = gr.MultimodalTextbox(label="Message", file_types=["image"], scale=7)
            clear = gr.ClearButton([msg, chatbot])

    app.load(lambda: (gr.Dropdown(choices=list_convs()), None), outputs=[conv_dd, current_conv])
    msg.submit(respond, [msg, chatbot, current_conv], [msg, chatbot, current_conv, conv_dd])
    new_btn.click(new_action, outputs=[chatbot, current_conv, conv_dd])
    conv_dd.change(switch_conv, [conv_dd], [chatbot, current_conv])
    del_btn.click(del_action, [current_conv], [chatbot, current_conv, conv_dd])

app.queue(default_concurrency_limit=5)
app.launch(server_name="0.0.0.0", server_port=7860, share=False)
