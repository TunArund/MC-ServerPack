"""发现、校验并加载电源 Provider。"""

from __future__ import annotations

import importlib.util
import inspect
import re
import sys
from pathlib import Path
from types import ModuleType


PROVIDERS_DIR = Path(__file__).resolve().parent / "providers"
PROVIDER_ID_RE = re.compile(r"[A-Za-z0-9_]+")
PROVIDER_ACTIONS = ("status", "start", "stop")
PROVIDER_PARAMETERS = ("instance_id", "region", "secret_id", "secret_key")


class ProviderError(RuntimeError):
    """Provider 不存在、无法加载或接口不符合约定。"""


def _source(provider: str, providers_dir: Path) -> Path:
    if not PROVIDER_ID_RE.fullmatch(provider):
        raise ProviderError("无效 provider ID；仅允许大小写字母、数字和下划线")
    return providers_dir / f"{provider}.py"


def list_providers(*, providers_dir: Path = PROVIDERS_DIR) -> tuple[str, ...]:
    """列出目录中命名合法的 Provider。"""
    if not providers_dir.is_dir():
        return ()
    return tuple(sorted(
        path.stem for path in providers_dir.glob("*.py")
        if path.name != "__init__.py" and PROVIDER_ID_RE.fullmatch(path.stem)
    ))


def check_provider(adapter: ModuleType, *, name: str = "provider") -> None:
    """检查 Provider 是否完整实现固定的四参数接口。"""
    for action in PROVIDER_ACTIONS:
        function = getattr(adapter, action, None)
        if not callable(function):
            raise ProviderError(f"provider {name} 未提供 {action}")
        try:
            signature = inspect.signature(function)
            signature.bind("instance", "region", "id", "key")
        except (TypeError, ValueError) as exc:
            raise ProviderError(f"provider {name} 的 {action} 接口不正确") from exc
        if tuple(signature.parameters) != PROVIDER_PARAMETERS:
            raise ProviderError(f"provider {name} 的 {action} 参数名不正确")


def load_provider(
    provider: str, *, providers_dir: Path = PROVIDERS_DIR,
) -> ModuleType:
    """从独立源码文件加载并校验 Provider。"""
    source = _source(provider, providers_dir)
    if not source.is_file():
        raise ProviderError(f"未找到 provider：{source.name}")
    module_name = f"{__package__}.providers._loaded_{provider}"
    spec = importlib.util.spec_from_file_location(module_name, source)
    if spec is None or spec.loader is None:
        raise ProviderError(f"provider 无法加载：{source.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        check_provider(module, name=source.name)
    except ProviderError:
        sys.modules.pop(module_name, None)
        raise
    except Exception as exc:
        sys.modules.pop(module_name, None)
        raise ProviderError(f"provider 加载失败：{source.name}：{exc}") from exc
    return module
