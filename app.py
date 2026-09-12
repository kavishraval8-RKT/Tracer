"""Tracer backend -- HTTP front end for the schematic generator.

Wraps `generator.py` in a small FastAPI service:

    GET  /health            liveness plus engine and toolchain versions
    POST /api/v1/generate   circuit definition in, .kicad_sch path out

The generator is a synchronous, CPU-bound module that keeps process-wide caches
and shells out to kicad-cli, so requests are handled on the threadpool and
serialised through one lock rather than run concurrently.

Run with:
    uvicorn app:app --reload --port 8000
"""

from __future__ import annotations

import io
import logging
import os
import re
import threading
import uuid
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from fastapi import FastAPI, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

import generator
from kicad_sch_api.core.exceptions import LibraryError

# --- configuration -----------------------------------------------------------

log = logging.getLogger("tracer")

HERE = Path(__file__).parent
OUTPUT_DIR = Path(os.environ.get("TRACER_OUTPUT_DIR", HERE / "generated")).resolve()

# Local development front ends. Tighten this before the service leaves localhost.
DEV_ORIGINS = [
    "http://localhost:3000", "http://127.0.0.1:3000",
    "http://localhost:5173", "http://127.0.0.1:5173",
    "http://localhost:8080", "http://127.0.0.1:8080",
]

# The generator mutates module-level caches and writes files named after the
# request, so one build runs at a time.
_BUILD_LOCK = threading.Lock()


# --- request schema ----------------------------------------------------------
#
# Mirrors mock_ai_output.json exactly, so that file can be POSTed unchanged:
# unknown top-level keys such as `_description` and `schema` are ignored, while
# everything inside `block` is validated strictly.


class Connection(BaseModel):
    """One pin of one component, joined to a net."""

    model_config = ConfigDict(extra="forbid")

    ref: str = Field(min_length=1, description="Reference designator, e.g. 'R1'")
    pin: str = Field(min_length=1, description="Pin number as it appears in the symbol")

    @field_validator("pin", mode="before")
    @classmethod
    def _pin_to_str(cls, value: Any) -> Any:
        # Pin numbers are strings in KiCad but arrive as ints often enough.
        return str(value) if isinstance(value, int) else value


class Net(BaseModel):
    """A named net. `class` is a Python keyword, hence the alias."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str = Field(min_length=1)
    net_class: Literal["signal", "power"] = Field("signal", alias="class")
    power_symbol: Optional[str] = Field(
        None, description="Override the library symbol used for a power rail")
    connections: List[Connection] = Field(min_length=1)

    @model_validator(mode="after")
    def _no_duplicate_pins(self) -> "Net":
        seen = {(c.ref, c.pin) for c in self.connections}
        if len(seen) != len(self.connections):
            raise ValueError(f"net '{self.name}' lists the same pin more than once")
        return self


class ComponentSpec(BaseModel):
    """One symbol placed by the layout engine."""

    model_config = ConfigDict(extra="forbid")

    ref: str = Field(min_length=1)
    value: str = ""
    symbol: str = Field(min_length=1, description="KiCad symbol id, e.g. 'Device:R'")
    footprint: Optional[str] = None
    column: int = Field(0, description="Signal-flow stage; ascending = left to right")
    row: int = Field(0, description="Vertical slot; 0 is the controller's row")
    rotation: Literal[0, 90, 180, 270] = 0
    role: Optional[Literal["controller"]] = None

    @field_validator("rotation", mode="before")
    @classmethod
    def _rotation_to_int(cls, value: Any) -> Any:
        if isinstance(value, float) and value.is_integer():
            return int(value)
        return value

    @field_validator("symbol")
    @classmethod
    def _symbol_has_library(cls, value: str) -> str:
        if value.count(":") != 1 or value.startswith(":") or value.endswith(":"):
            raise ValueError(f"symbol '{value}' must look like 'Library:SymbolName'")
        return value


class LayoutOptions(BaseModel):
    """Tuning knobs; every one has a worked-out default in the engine."""

    model_config = ConfigDict(extra="forbid")

    channel_mm: Optional[float] = Field(None, gt=0, le=100)
    row_pitch_mm: Optional[float] = Field(None, gt=0, le=200)
    body_margin_mm: Optional[float] = Field(None, ge=0, le=50)
    rail_drop_mm: Optional[float] = Field(None, gt=0, le=100)
    # Air between a wire and a symbol's drawn outline. 0 (the default) means
    # "may touch, must not cross", which is what schematic drawing requires.
    # Capped well under a grid step: anything larger pushes routes off the
    # 1.27 mm grid the rest of the engine is built on.
    body_clearance_mm: Optional[float] = Field(None, ge=0, le=1.27)

    def as_engine_dict(self) -> Dict[str, float]:
        return {k: v for k, v in self.model_dump().items() if v is not None}


class Block(BaseModel):
    """A circuit block: what to place, and what to wire together."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    sheet: Literal["A4", "A3", "A2", "A", "B"] = "A4"
    grid_mm: float = Field(generator.GRID_MM, gt=0, le=10)
    layout: LayoutOptions = Field(default_factory=LayoutOptions)
    components: List[ComponentSpec] = Field(min_length=1)
    nets: List[Net] = Field(default_factory=list)

    @model_validator(mode="after")
    def _cross_checks(self) -> "Block":
        refs = [c.ref for c in self.components]
        duplicates = sorted({r for r in refs if refs.count(r) > 1})
        if duplicates:
            raise ValueError(f"duplicate component refs: {', '.join(duplicates)}")

        controllers = [c.ref for c in self.components if c.role == "controller"]
        if len(controllers) > 1:
            raise ValueError(
                f"only one component may be the controller, got: {', '.join(controllers)}")

        known = set(refs)
        unknown = sorted({c.ref for net in self.nets for c in net.connections
                          if c.ref not in known})
        if unknown:
            raise ValueError(f"nets reference components that do not exist: "
                             f"{', '.join(unknown)}")

        names = [n.name for n in self.nets]
        repeated = sorted({n for n in names if names.count(n) > 1})
        if repeated:
            raise ValueError(f"duplicate net names: {', '.join(repeated)}")
        return self

    def as_engine_block(self) -> Dict[str, Any]:
        """Shape this into the plain dict `generator.build()` consumes."""
        return {
            "name": self.name,
            "sheet": self.sheet,
            "grid_mm": self.grid_mm,
            "layout": self.layout.as_engine_dict(),
            "components": [
                {k: v for k, v in c.model_dump().items() if v is not None}
                for c in self.components
            ],
            "nets": [
                {
                    "name": n.name,
                    "class": n.net_class,
                    **({"power_symbol": n.power_symbol} if n.power_symbol else {}),
                    "connections": [c.model_dump() for c in n.connections],
                }
                for n in self.nets
            ],
        }


class GenerateRequest(BaseModel):
    # Ignore rather than forbid unknown top-level keys, so mock_ai_output.json
    # (which carries `_description` and a `schema` block) can be POSTed as-is.
    model_config = ConfigDict(extra="ignore")

    block: Block
    filename: Optional[str] = Field(
        None, description="Output stem; defaults to a slug of the block name")

    @model_validator(mode="before")
    @classmethod
    def _accept_bare_block(cls, data: Any) -> Any:
        """Take a bare block as well as a wrapped one.

        `{"block": {...}}` is the documented shape, but posting just the block's
        own contents is the obvious mistake to make and costs nothing to accept.
        A payload carrying `components` at the top level can only be a block.
        """
        if isinstance(data, dict) and "block" not in data and "components" in data:
            return {"block": data}
        return data


# --- response schema ---------------------------------------------------------


class ErcViolation(BaseModel):
    severity: str
    type: str
    description: str
    items: List[str] = Field(default_factory=list)


class ErcReport(BaseModel):
    ran: bool = Field(description="False when kicad-cli could not be reached")
    clean: bool
    violations: List[ErcViolation] = Field(default_factory=list)


class Stats(BaseModel):
    components: int
    power_symbols: int
    power_flags: int
    wires: int
    junctions: int
    labels: int
    nets: int


class GenerateResponse(BaseModel):
    status: Literal["ok", "warning"]
    schematic_path: str
    filename: str
    engine_version: str
    stats: Stats
    erc: ErcReport
    log: List[str]


class FieldError(BaseModel):
    field: str = Field(description="Dotted path to the offending field")
    message: str


class ErrorResponse(BaseModel):
    status: Literal["error"] = "error"
    stage: str = Field(description="Which phase failed: request, layout, verification, ...")
    detail: str
    hint: Optional[str] = None
    errors: List[FieldError] = Field(
        default_factory=list, description="Per-field problems, when the request "
                                          "itself failed validation")


class HealthResponse(BaseModel):
    status: Literal["healthy"] = "healthy"
    engine_version: str
    kicad_sch_api_version: str
    erc_available: bool
    output_dir: str


# --- generation --------------------------------------------------------------


class GenerationError(Exception):
    """A build that failed for a reason the caller can act on."""

    def __init__(self, stage: str, detail: str, hint: Optional[str] = None) -> None:
        super().__init__(detail)
        self.stage = stage
        self.detail = detail
        self.hint = hint


_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_stem(name: str) -> str:
    """Turn a caller-supplied name into a filename that stays in OUTPUT_DIR."""
    stem = _SLUG_RE.sub("_", name).strip("._-")
    return (stem or "circuit")[:60]


def _collect_stats(sch, block: Dict[str, Any]) -> Stats:
    components = list(sch.components)
    flags = [c for c in components if c.lib_id == "power:PWR_FLAG"]
    rails = [c for c in components
             if c.lib_id.startswith("power:") and c.lib_id != "power:PWR_FLAG"]
    return Stats(
        components=len(components) - len(flags) - len(rails),
        power_symbols=len(rails),
        power_flags=len(flags),
        wires=len(list(sch.wires)),
        junctions=len(list(sch.junctions)),
        labels=len(list(sch.labels)),
        nets=len(block["nets"]),
    )


def _erc_report(path: Path) -> ErcReport:
    violations = generator.collect_erc(path)
    if violations is None:
        return ErcReport(ran=False, clean=True)
    return ErcReport(
        ran=True,
        clean=not violations,
        violations=[
            ErcViolation(
                severity=v.get("severity", "unknown"),
                type=v.get("type", "unknown"),
                description=v.get("description", ""),
                items=[i.get("description", "") for i in v.get("items", [])],
            )
            for v in violations
        ],
    )


def generate_schematic(request: GenerateRequest) -> GenerateResponse:
    """Run the engine end to end and describe what came out.

    Every failure mode the engine has is mapped onto a stage the caller can
    understand, because "500 internal error" tells nobody which pin was wrong.
    """
    block = request.block.as_engine_block()
    stem = safe_stem(request.filename or request.block.name)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / f"{stem}-{uuid.uuid4().hex[:8]}.kicad_sch"

    buffer = io.StringIO()
    with _BUILD_LOCK:
        try:
            with redirect_stdout(buffer):
                sch = generator.build(block)
                # The file lands on disk before the rendered-text audit can read
                # it back, so any failure past this point leaves a partial file
                # that must not survive the request.
                sch.save(str(out_path))
                generator.audit_rendered_text(out_path, block)
        except BaseException as exc:
            out_path.unlink(missing_ok=True)
            if isinstance(exc, LibraryError):
                raise GenerationError(
                    "symbol-lookup", str(exc),
                    "Check each component's `symbol` against the installed KiCad "
                    "libraries, e.g. 'Device:R'.") from exc
            if isinstance(exc, KeyError):
                raise GenerationError(
                    "pin-lookup", str(exc.args[0] if exc.args else exc),
                    "A net names a pin the symbol does not have. Pin numbers are "
                    "the symbol's own, not sequential.") from exc
            if isinstance(exc, AssertionError):
                # Every verification stage in the engine reports this way.
                raise GenerationError(
                    "verification", str(exc),
                    "The layout was built but failed its geometry or connectivity "
                    "checks. Adjust the offending part's column/row/rotation.") from exc
            if isinstance(exc, ValueError):
                raise GenerationError("layout", str(exc)) from exc
            raise

        erc = _erc_report(out_path)
        stats = _collect_stats(sch, block)

    log = [line for line in buffer.getvalue().splitlines()
           if line.strip() and not line.startswith("Symbol ")]
    return GenerateResponse(
        status="ok" if erc.clean else "warning",
        schematic_path=str(out_path),
        filename=out_path.name,
        engine_version=generator.ENGINE_VERSION,
        stats=stats,
        erc=erc,
        log=log,
    )


# --- application -------------------------------------------------------------

app = FastAPI(
    title="Tracer Engine",
    version=generator.ENGINE_VERSION,
    summary="Turn a circuit block definition into a KiCad schematic.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=DEV_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.exception_handler(GenerationError)
async def _generation_error_handler(_, exc: GenerationError) -> JSONResponse:
    log.warning("generate failed at %s: %s", exc.stage, exc.detail)
    body = ErrorResponse(stage=exc.stage, detail=exc.detail, hint=exc.hint)
    return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        content=body.model_dump(exclude_none=True))


@app.exception_handler(RequestValidationError)
async def _validation_error_handler(_, exc: RequestValidationError) -> JSONResponse:
    """Say which fields were wrong, in the response *and* in the server log.

    A bare "422 Unprocessable Content" in the console tells nobody anything, and
    the reason is otherwise only visible to whoever reads the response body.
    """
    errors = [
        FieldError(
            # Drop the leading "body" segment; it is the same for every entry.
            field=".".join(str(part) for part in err.get("loc", ())[1:]) or "body",
            message=err.get("msg", "invalid"),
        )
        for err in exc.errors()
    ]
    summary = "; ".join(f"{e.field}: {e.message}" for e in errors[:4])
    if len(errors) > 4:
        summary += f" (+{len(errors) - 4} more)"
    log.warning("rejected request: %s", summary)

    body = ErrorResponse(
        stage="request-validation",
        detail=summary or "request did not match the expected schema",
        hint="POST the whole mock_ai_output.json, or just its `block` object. "
             "The Swagger 'Try it out' placeholders are not a valid circuit.",
        errors=errors,
    )
    return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        content=body.model_dump(exclude_none=True))


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    """Point a bare browser hit at the interactive docs."""
    return RedirectResponse(url="/docs")


@app.get("/health", response_model=HealthResponse, tags=["meta"])
def health() -> HealthResponse:
    """Liveness, plus what the engine can actually do on this host."""
    import kicad_sch_api

    return HealthResponse(
        engine_version=generator.ENGINE_VERSION,
        kicad_sch_api_version=getattr(kicad_sch_api, "__version__", "unknown"),
        erc_available=generator.erc_available(),
        output_dir=str(OUTPUT_DIR),
    )


@app.post(
    "/api/v1/generate",
    response_model=GenerateResponse,
    responses={422: {"model": ErrorResponse}},
    tags=["generation"],
)
def generate(request: GenerateRequest) -> GenerateResponse:
    """Lay out, route and verify a circuit block, then write it to disk.

    Deliberately `def`, not `async def`: the engine is synchronous and CPU
    bound, so FastAPI runs it on the threadpool instead of blocking the loop.
    """
    return generate_schematic(request)
