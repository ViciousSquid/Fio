"""A plugin written against API 1.4.0, using what 1.4.0 offers.

It must load and behave exactly as before on a newer host: 1.5.0 is additive.
"""

from plugins.api import FioPlugin


class Api14Plugin(FioPlugin):
    name = "api_1_4_plugin"
    version = "1.0.0"
    api_version = "1.4.0"
    description = "Registers a property tab, a Tools action and a console command."
    category = "Tests"
    enabled = True

    def __init__(self):
        self.commands = []

    def register(self, api):
        api.register_property_tab("Legacy Tab", lambda thing: None,
                                  entity_type="api14entity")
        api.register_tools_action("Legacy Tool", lambda main_window: None)
        api.register_console_command("legacy", self._on_command, "a 1.4 command")

    def _on_command(self, args, main_window, logic, play_mode):
        self.commands.append(args)
        return "legacy:" + args


PLUGIN = Api14Plugin()
