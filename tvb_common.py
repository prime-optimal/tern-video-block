"""Shared between tern-video-block and its submodules: PlayError, the one exception the player raises."""


class PlayError(Exception):
    """A file or a terminal the player cannot play in; the message says which and why."""