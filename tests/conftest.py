import sys
from pathlib import Path
from types import ModuleType

PLUGIN_DIR = (
    Path(__file__).resolve().parents[1] / "my-bot" / "plugins" / "multi_llm_chat"
)


package = ModuleType("multi_llm_chat")
package.__path__ = [str(PLUGIN_DIR)]
package.__package__ = "multi_llm_chat"
sys.modules.setdefault("multi_llm_chat", package)

wife_package = ModuleType("peteralbus_wife")
wife_package.__path__ = [str(PLUGIN_DIR.parent / "peteralbus_wife")]
wife_package.__package__ = "peteralbus_wife"
sys.modules.setdefault("peteralbus_wife", wife_package)
