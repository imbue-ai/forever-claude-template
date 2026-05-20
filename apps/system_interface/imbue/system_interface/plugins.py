import pluggy

from imbue.system_interface.hookspecs import SystemInterfaceHookSpec

_plugin_manager: pluggy.PluginManager | None = None


def get_plugin_manager() -> pluggy.PluginManager:
    global _plugin_manager
    if _plugin_manager is None:
        _plugin_manager = pluggy.PluginManager("system_interface")
        _plugin_manager.add_hookspecs(SystemInterfaceHookSpec)
    return _plugin_manager
