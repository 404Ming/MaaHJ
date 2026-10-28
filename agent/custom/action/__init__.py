from importlib import import_module

ACTION_MODULES = (
    "general",
    "gameplay_challenge",
    "machine_god_descent",
    "dungeon_challenge",
    "auto_idle",
    "auto_idle_battle",
    "final_battle",
    "shop",
    "alchemy",
    "stamina_refill",
    "weekly_dispatch",
    "daily",
)


def register_all() -> None:
    for module_name in ACTION_MODULES:
        import_module(f"custom.action.{module_name}")


__all__ = ["register_all"]
