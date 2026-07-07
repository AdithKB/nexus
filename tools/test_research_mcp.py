import asyncio
import json
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

async def test_research():
    print("🧪 Testing 'research_to_vault' MCP Tool...")
    
    server_script = Path(__file__).parent.parent / "mcp_nexus.py"
    
    # Start server
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(server_script),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=1024 * 1024
    )

    async def send_req(method, params=None, id=1):
        req = {"jsonrpc": "2.0", "method": method, "params": params or {}, "id": id}
        process.stdin.write(json.dumps(req).encode() + b"\n")
        await process.stdin.drain()
        
        while True:
            line = await process.stdout.readline()
            if not line:
                return None
            line_str = line.decode().strip()
            if not line_str:
                continue
            # Try to find the JSON-RPC object
            if line_str.startswith("{"):
                try:
                    return json.loads(line_str)
                except json.JSONDecodeError:
                    continue
            else:
                # This is a log line from the server
                print(f"  [Server Log] {line_str}")

    try:
        # 1. Init
        await send_req("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1.0"}
        }, id=0)
        print("✅ Server Initialized.")

        # 2. Call Research Tool
        # We use a stable URL about MCP itself
        test_url = "https://modelcontextprotocol.io/introduction"
        print(f"📡 Calling 'research_to_vault' for: {test_url}")
        print("⏳ This involves Stage 1 (Gemini) and Stage 2 (Ollama/MLX). Please wait...")
        
        resp = await send_req("tools/call", {
            "name": "research_to_vault",
            "arguments": {
                "url": test_url,
                "user_intent": "extract core architecture principles"
            }
        })

        if "result" in resp:
            content = resp["result"]["content"][0]["text"]
            print("\n🎉 SUCCESS!")
            print(content)
        else:
            print(f"\n❌ FAILED: {resp.get('error', 'Unknown error')}")

    except Exception as e:
        print(f"💥 Crash: {e}")
    finally:
        process.terminate()
        await process.wait()

if __name__ == "__main__":
    asyncio.run(test_research())
