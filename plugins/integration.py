
    _orig_create_menu_bar = Ui_MainWindow.create_menu_bar

    def create_menu_bar(self, MainWindow):
        _orig_create_menu_bar(self, MainWindow)
        try:
            from PyQt5.QtWidgets import QAction
            from plugins.manager import get_manager

            mgr = get_manager()
            records = mgr.tools_actions()
            if not records:
                return

            installed = []
            for plugin, label, callback, tooltip in records:
                action = QAction(label, MainWindow)
                if tooltip:
                    action.setToolTip(tooltip)
                action.triggered.connect(
                    lambda _checked=False, p=plugin, cb=callback, mw=MainWindow:
                    cb(mw) if mgr.is_enabled(p) else None
                )
                MainWindow.tools_menu.insertAction(MainWindow.logic_graph_action, action)
                installed.append((plugin, action))

            def refresh_visibility():
                for plugin, action in installed:
                    action.setVisible(mgr.is_enabled(plugin))

            refresh_visibility()
            MainWindow.tools_menu.aboutToShow.connect(refresh_visibility)
            MainWindow._fio_plugin_tools_actions = installed
        except Exception as exc:
            _log(f"plugin Tools action setup failed: {exc}")

    Ui_MainWindow.create_menu_bar = create_menu_bar
    Ui_MainWindow._fio_plugin_tools_patched = True


# ---------------------------------------------------------------------------
# editor.property_editor.PropertyEditor  — typed widgets from a property schema
# ---------------------------------------------------------------------------

def _patch_property_editor():
    """Render plugin-declared property schemas with fitting widgets.