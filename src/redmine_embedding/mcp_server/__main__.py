import argparse
import logging
import sys

from ..common.config import add_config_arguments, configure_from_args, get_settings
from ..common.settings_file import SettingsFileError


LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="redmine-embedding-mcp")
    add_config_arguments(parser)
    parser.add_argument("--transport", choices=["streamable-http", "stdio"], default="streamable-http")
    parser.add_argument("--host", help="listen host (default: settings mcp_host)")
    parser.add_argument("--port", type=int, help="listen port (default: settings mcp_port)")
    parser.add_argument(
        "--allow-dev-token",
        action="store_true",
        help="development only: use the REDMINE_MCP_DEV_TOKEN environment variable when no Authorization header is sent",
    )
    args = parser.parse_args(argv)
    try:
        configure_from_args(args)
        settings = get_settings()
    except SettingsFileError as e:
        print(e, file=sys.stderr)
        sys.exit(2)

    host = args.host or settings.mcp_host
    if args.allow_dev_token and args.transport != "stdio" and host not in LOOPBACK_HOSTS:
        print(
            f"--allow-dev-token は 127.0.0.1 / ::1 / localhost での待受時のみ使えます（host={host}）",
            file=sys.stderr,
        )
        sys.exit(2)

    from ..common.requirements import missing_items, report_and_exit_code

    oauth_mode = args.transport != "stdio" and bool(settings.mcp_public_base_url)
    code = report_and_exit_code(missing_items(settings, "mcp", oauth=oauth_mode), "redmine-embedding-mcp")
    if code:
        sys.exit(code)

    from .auth.token import make_token_source
    from .server import create_server

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.allow_dev_token:
        logging.getLogger(__name__).warning("--allow-dev-token: development token enabled; never use in production")
    if args.transport != "stdio" and settings.mcp_public_base_url:
        _run_oauth(args, settings, host)
        return
    server = create_server(settings, token_source=make_token_source(allow_dev_token=args.allow_dev_token))
    if args.transport == "stdio":
        server.run("stdio")
    else:
        server.run("streamable-http", host=host, port=args.port or settings.mcp_port)


def _run_oauth(args, settings, host: str) -> None:
    """OAuth2 mode: this server is its own authorization server (needs DB + env secrets)."""
    import uvicorn

    from .auth.config import OAuthConfigError
    from .auth.http import build_http_app
    from .auth.setup import build_oauth
    from .auth.token import make_token_source
    from .server import create_server

    if args.allow_dev_token:
        print("--allow-dev-token は OAuth2 モード（mcp_public_base_url 設定時）では使えません", file=sys.stderr)
        sys.exit(2)
    try:
        oauth = build_oauth(settings)
    except OAuthConfigError as e:  # names the missing item, never a value
        print(e, file=sys.stderr)
        sys.exit(2)
    try:
        server = create_server(settings, token_source=make_token_source(oauth=True), oauth=oauth)
        app = build_http_app(server, oauth.provider, host=host)
        uvicorn.run(app, host=host, port=args.port or settings.mcp_port, log_level="info", access_log=False)  # no access log: /oauth/callback carries the upstream code in its query
    finally:
        if oauth.close:
            oauth.close()


if __name__ == "__main__":
    main()
