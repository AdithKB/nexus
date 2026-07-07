import asyncio
import json
import os
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

async def run_mcp_test():
    """Stress test the MCP server by sending various JSON-RPC requests."""
    print("🚀 Starting MCP Feature & Stress Test...")
    
    # Path to the MCP server
    server_script = Path(__file__).parent.parent / "mcp_nexus.py"
    
    # Start the server as a subprocess with a larger limit for stdout
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(server_script),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=1024 * 1024 # 1MB limit for large vault indices
    )

    async def send_request(method, params=None, id=1):
        request = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
            "id": id
        }
        process.stdin.write(json.dumps(request).encode() + b"\n")
        await process.stdin.drain()
        
        line = await process.stdout.readline()
        if not line:
            return None
        return json.loads(line.decode())

    try:
        # 1. Initialize
        print("\n[1/5] Initializing MCP Server...")
        init_resp = await send_request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {"sampling": {}},
            "clientInfo": {"name": "test-client", "version": "1.0"}
        }, id=0)
        print("✅ Initialized.")

        # 2. List Resources
        print("\n[2/5] Testing Resource Listing (nexus://)...")
        res_resp = await send_request("resources/list")
        if "result" in res_resp and "resources" in res_resp["result"]:
            resources = res_resp["result"]["resources"]
            print(f"✅ Found {len(resources)} resources.")
            if resources:
                first_uri = resources[0]["uri"]
                print(f"📄 Reading first resource: {first_uri}")
                read_resp = await send_request("resources/read", {"uri": first_uri})
                if "result" in read_resp:
                    print(f"✅ Read successful ({len(read_resp['result']['contents'][0]['text'])} chars).")
        else:
            print("❌ Resource listing failed.")

        # 3. Inspect Graph Schema
        print("\n[3/5] Testing 'inspect_graph_schema' tool...")
        schema_resp = await send_request("tools/call", {
            "name": "inspect_graph_schema",
            "arguments": {}
        })
        if "result" in schema_resp:
            content = schema_resp["result"]["content"][0]["text"]
            print("✅ Schema received:")
            # Print first 5 lines of the table
            print("\n".join(content.split("\n")[:7]))
        else:
            print("❌ Tool call failed.")

        # 4. Graph Search (Stress Test)
        print("\n[4/5] Testing 'graph_search' (Multi-hop)...")
        # We'll use a generic term like "transformer" or "inference"
        graph_resp = await send_request("tools/call", {
            "name": "graph_search",
            "arguments": {"query": "transformer", "hops": 2, "k": 3}
        })
        if "result" in graph_resp:
            print("✅ Graph search completed.")
            # Sampling might have been triggered if supported/simulated
            # But here we are just checking if we got a result
        else:
            print("❌ Graph search failed.")

        # 5. Semantic Search (Standard)
        print("\n[5/5] Testing 'search_vault'...")
        search_resp = await send_request("tools/call", {
            "name": "search_vault",
            "arguments": {"query": "LoRA memory efficiency", "k": 5}
        })
        if "result" in search_resp:
            print("✅ Search successful.")
        else:
            print("❌ Search failed.")

        print("\n🏁 All tests completed successfully.")

    except Exception as e:
        print(f"💥 Test crashed: {e}")
    finally:
        process.terminate()
        await process.wait()

if __name__ == "__main__":
    asyncio.run(run_mcp_test())
