"""Finto HuskyLens 2: server MCP via SSE su http://127.0.0.1:3000/sse (solo per test)."""
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("huskylens-finto")

@mcp.tool()
def get_recognition_result() -> str:
    """Restituisce i risultati di riconoscimento del modello attivo."""
    return '{"model": "face_recognition", "faces": [{"id": 1, "name": "Alex"}]}'

@mcp.tool()
def manage_applications(operation: str, name: str = "") -> str:
    """Elenca o cambia il modello attivo. operation: list | switch."""
    return f"ok: {operation} {name}"

if __name__ == "__main__":
    mcp.run(transport="sse", host="127.0.0.1", port=3000)
