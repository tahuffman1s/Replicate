"""The keyboard shortcuts overlay (Ctrl+?)."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

_UI = """
<interface>
  <object class="GtkShortcutsWindow" id="window">
    <property name="modal">true</property>
    <child>
      <object class="GtkShortcutsSection">
        <property name="section-name">shortcuts</property>
        <property name="max-height">12</property>
        <child>
          <object class="GtkShortcutsGroup">
            <property name="title">General</property>
            <child><object class="GtkShortcutsShortcut"><property name="accelerator">&lt;Primary&gt;question</property><property name="title">Keyboard shortcuts</property></object></child>
            <child><object class="GtkShortcutsShortcut"><property name="accelerator">&lt;Primary&gt;comma</property><property name="title">Settings</property></object></child>
            <child><object class="GtkShortcutsShortcut"><property name="accelerator">&lt;Primary&gt;q</property><property name="title">Quit</property></object></child>
          </object>
        </child>
        <child>
          <object class="GtkShortcutsGroup">
            <property name="title">Pages</property>
            <child><object class="GtkShortcutsShortcut"><property name="accelerator">&lt;Alt&gt;1</property><property name="title">Overview</property></object></child>
            <child><object class="GtkShortcutsShortcut"><property name="accelerator">&lt;Alt&gt;2</property><property name="title">SQL Servers</property></object></child>
            <child><object class="GtkShortcutsShortcut"><property name="accelerator">&lt;Alt&gt;3</property><property name="title">Images and Clones</property></object></child>
            <child><object class="GtkShortcutsShortcut"><property name="accelerator">&lt;Alt&gt;4</property><property name="title">Activity</property></object></child>
            <child><object class="GtkShortcutsShortcut"><property name="accelerator">&lt;Alt&gt;5</property><property name="title">Settings</property></object></child>
          </object>
        </child>
        <child>
          <object class="GtkShortcutsGroup">
            <property name="title">On a page</property>
            <child><object class="GtkShortcutsShortcut"><property name="accelerator">&lt;Primary&gt;r</property><property name="title">Refresh (rescan images, check servers)</property></object></child>
            <child><object class="GtkShortcutsShortcut"><property name="accelerator">&lt;Primary&gt;n</property><property name="title">Add (server, image folder)</property></object></child>
          </object>
        </child>
      </object>
    </child>
  </object>
</interface>
"""


def ShortcutsWindow(parent: Gtk.Window) -> Gtk.ShortcutsWindow:  # noqa: N802 - reads like a widget
    builder = Gtk.Builder.new_from_string(_UI, -1)
    window: Gtk.ShortcutsWindow = builder.get_object("window")
    window.set_transient_for(parent)
    return window
