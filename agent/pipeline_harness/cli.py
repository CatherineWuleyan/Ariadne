from __future__ import annotations

import argparse
import copy
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .checks import check_run
from .runner import run_pipeline
from .store import RunStore, read_json
from .synthetic import BUILTIN_PIPELINES
from .view.model import ViewDocument, search_view
from .view.projector import _render_html, project_run


DEFAULT_RUNS_ROOT = Path(__file__).resolve().parents[2] / "outputs" / "harness_runs"


def _load_pipeline(value: str) -> dict[str, Any]:
    if value in BUILTIN_PIPELINES:
        return copy.deepcopy(BUILTIN_PIPELINES[value])
    path = Path(value).resolve()
    payload = read_json(path)
    return payload


def _json_print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pipeline_harness",
        description="Pipeline-independent local harness kernel",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="create a run from a pipeline config")
    init_parser.add_argument("--pipeline", required=True, help="built-in name or JSON config path")
    init_parser.add_argument("--runs-root", type=Path, default=DEFAULT_RUNS_ROOT)
    init_parser.add_argument(
        "--inputs",
        type=Path,
        help="JSON manifest of external files to freeze into the run",
    )

    fork_parser = subparsers.add_parser(
        "fork", help="create a child run from a successful checkpoint"
    )
    fork_parser.add_argument("--run", required=True, type=Path, dest="run_dir")
    fork_parser.add_argument("--checkpoint", required=True, dest="checkpoint_id")
    fork_parser.add_argument("--reason")
    fork_parser.add_argument(
        "--inputs",
        type=Path,
        dest="additional_input_manifest",
        help="JSON manifest of additional inputs to freeze into the child run",
    )

    run_parser = subparsers.add_parser("run", help="run or resume configured stages")
    run_parser.add_argument("--run", required=True, type=Path, dest="run_dir")
    run_parser.add_argument("--max-stages", type=int)

    status_parser = subparsers.add_parser("status", help="show persisted run state")
    status_parser.add_argument("--run", required=True, type=Path, dest="run_dir")

    view_parser = subparsers.add_parser("view", help="project a standalone read-only view")
    view_parser.add_argument("--run", required=True, type=Path, dest="run_dir")
    view_parser.add_argument("--adapter", help="override adapter with module:object")

    viewer_parser = subparsers.add_parser("serve-viewer", help="serve the shared viewer for one run or view model")
    viewer_source = viewer_parser.add_mutually_exclusive_group(required=True)
    viewer_source.add_argument("--run", type=Path, dest="viewer_run_dir")
    viewer_source.add_argument("--view-model", type=Path, dest="viewer_model_path")
    viewer_parser.add_argument("--port", type=int, default=0)

    search_parser = subparsers.add_parser("search", help="search the projected view")
    search_parser.add_argument("--run", required=True, type=Path, dest="run_dir")
    search_parser.add_argument("--query", required=True)

    check_parser = subparsers.add_parser(
        "check", help="check run, artifacts, checkpoints and view"
    )
    check_parser.add_argument("--run", required=True, type=Path, dest="run_dir")
    return parser


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "init":
        store = RunStore.create(
            args.runs_root,
            _load_pipeline(args.pipeline),
            input_manifest=args.inputs,
        )
        print(store.run_dir)
        return 0
    if args.command == "fork":
        store = RunStore.fork(
            args.run_dir,
            args.checkpoint_id,
            reason=args.reason,
            additional_input_manifest=args.additional_input_manifest,
        )
        print(store.run_dir)
        return 0
    if args.command == "run":
        run = run_pipeline(args.run_dir, max_stages=args.max_stages)
        _json_print(run.to_dict())
        return 0 if run.status in {"succeeded", "waiting"} else 1
    if args.command == "status":
        _json_print(RunStore(args.run_dir).load_run().to_dict())
        return 0
    if args.command == "view":
        project_run(args.run_dir, adapter_spec=args.adapter)
        print((args.run_dir.resolve() / "views/viewer.html"))
        return 0
    if args.command == "serve-viewer":
        if args.viewer_run_dir is not None:
            view = project_run(args.viewer_run_dir, write_viewer=False)
        else:
            view = ViewDocument.from_dict(read_json(args.viewer_model_path))
        html = _render_html(view)
        payload = json.dumps(view.to_dict(), ensure_ascii=False).encode("utf-8")

        class ViewerHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path.split("?", 1)[0] == "/view_model.json":
                    body, content_type = payload, "application/json; charset=utf-8"
                else:
                    body, content_type = html, "text/html; charset=utf-8"
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *values: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", args.port), ViewerHandler)
        host, port = server.server_address[:2]
        print(f"http://{host}:{port}/?dev=1", flush=True)
        try:
            server.serve_forever()
        finally:
            server.server_close()
        return 0
    if args.command == "search":
        path = args.run_dir.resolve() / "views/view_model.json"
        if not path.is_file():
            raise ValueError("view_model.json does not exist; run the view command first")
        view = ViewDocument.from_dict(read_json(path))
        _json_print(search_view(view, args.query))
        return 0
    if args.command == "check":
        findings = check_run(args.run_dir)
        _json_print(
            {
                "ok": not any(item.severity == "error" for item in findings),
                "findings": [item.to_dict() for item in findings],
            }
        )
        return 1 if any(item.severity == "error" for item in findings) else 0
    raise AssertionError(f"unknown command: {args.command}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "error": type(exc).__name__, "message": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
