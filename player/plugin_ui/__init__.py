"""Player presentation of the plugins that ship with Fio.

The player ships the same plugins as desktop Fio -- Bigworld, Deathmatch
(multiplayer) and Tidy -- because a ``.fiopak`` world may not carry plugin
code. Their rules run unchanged on the shared engine the player hosts.
Desktop Fio presents some of them through its editor: Qt overlay painters,
debug-console commands and Qt dialogs. This package is the player's
presentation of the same plugins, drawn on the touch HUD and pause menu:

* :mod:`.arena` and :mod:`.scoreboard` -- the Deathmatch session, scoreboard
  and match controls;
* :mod:`.lobby` -- Deathmatch LAN discovery, hosting and joining;
* :mod:`.bigworld` -- Bigworld's cell debug panel and minimap.

Desktop sources are never modified; plugins are reached through the plugin
manager's own seams.
"""
