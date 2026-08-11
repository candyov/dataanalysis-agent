import asyncio, json, sys
import httpx

async def test():
    body = {
        "message": "分析这份销售数据，告诉我核心发现和行动建议",
        "file_path": "D:\\Desktop\\ai-data-analysis\\backend\\storage\\uploads\\4d416cd1_sales_data.csv",
        "session_id": "sess_e2e_full",
    }
    
    url = "http://localhost:8010/api/v1/chat"
    events = []
    
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=10)) as client:
            async with client.stream("POST", url, json=body) as resp:
                print(f"Status: {resp.status_code}", flush=True)
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    try:
                        evt = json.loads(line[6:])
                        t = evt.get("type", "?")
                        events.append((t, json.dumps(evt, ensure_ascii=False)[:300]))
                        if t in ("stage", "summary", "bot", "tool_call", "error", "done", "complete"):
                            print(f"  [{t}] {json.dumps(evt, ensure_ascii=False)[:250]}", flush=True)
                    except:
                        pass
    except Exception as e:
        print(f"ERROR: {e}", flush=True)
    
    print(f"\n--- Done: {len(events)} events ---", flush=True)
    with open("D:\\Desktop\\ai-data-analysis\\backend\\test_result.json", "w", encoding="utf-8") as f:
        json.dump(events, f, ensure_ascii=False, indent=2)

asyncio.run(test())
