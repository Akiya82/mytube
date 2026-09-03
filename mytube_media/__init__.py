"""Media knowledge shared by two programs: the interface (installs `[api]`,
FastAPI and asyncpg included) and the converter (installs `[worker]`,
neither of those). One rule holds for every module in this package and for
any module added to it later: nothing here may import from `orchestrator`,
from any web framework, database driver or settings library, or from any
dependency outside the small shared set both programs install. A single
import that violates this quietly makes the converter's install carry the
interface's whole dependency tree -- the split `pyproject.toml` expresses
between the two programs, defeated by one convenience import that lives on
only one side.
"""
