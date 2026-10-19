import socket
import sys


def main() -> int:
    for raw in sys.argv[1:]:
        port = int(raw)
        try:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", port))
        except PermissionError:
            print(f"Cannot check port {port}: this environment denied local socket access. Run the dev server in an environment that permits local TCP sockets.", file=sys.stderr)
            return 1
        except OSError:
            print(f"Port {port} is already occupied. Stop the process using it or choose another port.", file=sys.stderr)
            return 1
    print("Development ports are available.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
