"""Finti server MCP per i test: Streamable HTTP (porta indicata) oppure stdio.

Uso:  python tests/fake_mcp_servers.py http 18901      python tests/fake_mcp_servers.py stdio
"""
import sys

from mcp.server.mcpserver import MCPServer

mode = sys.argv[1] if len(sys.argv) > 1 else "stdio"
mcp = MCPServer(f"finto-{mode}")


@mcp.tool()
def somma(a: int, b: int) -> str:
    """Somma due numeri interi."""
    return f"{a + b} (calcolato da {mode})"


@mcp.tool()
def saluto(nome: str = "classe") -> str:
    """Restituisce un saluto."""
    return f"Ciao {nome} da {mode}"


if __name__ == "__main__":
    if mode == "http":
        mcp.run(transport="streamable-http", host="127.0.0.1", port=int(sys.argv[2]))
    else:
        mcp.run(transport="stdio")
