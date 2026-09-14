"""Live acceptance with the synthetic PostgreSQL warehouse; creates one test conversation."""
import argparse
import json
import sys
import uuid
from contextlib import closing
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.core import db
from backend.auth import create_session, revoke_session, SESSION_COOKIE


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser=argparse.ArgumentParser()
    parser.add_argument("--username",required=True)
    args=parser.parse_args()
    with closing(db()) as con:
        user=con.execute("SELECT id FROM users WHERE username=? AND is_active=1",(args.username,)).fetchone()
    if not user:
        raise SystemExit("Active user not found")
    token=create_session(user["id"])
    try:
        with httpx.Client(base_url="http://127.0.0.1:18001",trust_env=False,timeout=180,cookies={SESSION_COOKIE:token}) as client:
            created=client.post("/api/conversations");created.raise_for_status();cid=created.json()["id"]
            print("Acceptance conversation: "+cid,flush=True)
            def chat(question):
                response=client.post("/api/chat/stream",json={"session_id":cid,"client_message_id":str(uuid.uuid4()),"message":question})
                response.raise_for_status()
                events=[]
                for block in response.text.split("\n\n"):
                    lines=block.splitlines()
                    event=next((line[6:].strip() for line in lines if line.startswith("event:")),"")
                    data=next((line[5:].strip() for line in lines if line.startswith("data:")),"")
                    if data: events.append((event,json.loads(data)))
                errors=[data for event,data in events if event=="error"]
                assert not errors,errors
                done=next(data for event,data in events if event=="done")
                print(json.dumps({"question":question,"answer":done["answer"],"visualizations":done.get("visualizations"),
                                  "nodes":[n["node"] for n in (done.get("trace") or {}).get("nodes",[])]},ensure_ascii=False),flush=True)
                return done
            count=chat("查询 PostgreSQL 测试数仓 ods.orders 有多少行")
            assert count["visualizations"][0]["chart_type"]=="card",count
            grouped=chat("查询 PostgreSQL 测试数仓 ods.orders 按 status 分组统计记录数，使用柱状图展示。")
            assert grouped["visualizations"][0]["chart_type"]=="bar",grouped
            assert len(grouped["result"])>1
            changed=chat("把刚才的柱状图改成环形图，使用相同的数据，不要重新查询。")
            assert changed["visualizations"][0]["chart_type"]=="pie",changed
            assert changed["result"]==grouped["result"]
            assert not any(n["node"] in {"query","data_query","rag"} for n in changed["trace"]["nodes"]),changed["trace"]
            restored=client.get(f"/api/conversations/{cid}/messages");restored.raise_for_status()
            answers=[m for m in restored.json()["messages"] if m["role"]=="assistant"]
            assert answers[-1]["payload"]["visualizations"]==changed["visualizations"]
            regrouped=chat("重新按 channel 分组统计 ods.orders 的记录数，展示柱状图。")
            assert any(n["node"]=="query" for n in regrouped["trace"]["nodes"]),regrouped["trace"]
            assert regrouped["visualizations"][0]["x"]=="channel",regrouped["visualizations"]
            print("PASS live: count card, PostgreSQL grouped bar, model-routed pie followup without SQL, persisted chart",flush=True)
    finally:
        revoke_session(token)


if __name__=="__main__":
    main()
