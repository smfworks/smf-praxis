from hybridagent import tui


def test_menu_renders_and_has_quit():
    menu = tui.render_menu()
    assert "Praxis" in menu
    assert any(key == "quit" for _label, key in tui.MENU)
    assert any(key == "onboard" for _label, key in tui.MENU)


def test_color_disabled_without_tty():
    # Not a TTY under pytest, so styling is a no-op (plain text).
    assert tui._bold("x") == "x"
