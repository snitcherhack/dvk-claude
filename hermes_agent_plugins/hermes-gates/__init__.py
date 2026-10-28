from .bridge import handle_gate, handle_gates


def register(ctx):
    ctx.register_command(
        "gates",
        handler=handle_gates,
        description="List pending Hermes human gates",
    )
    ctx.register_command(
        "gate",
        handler=handle_gate,
        description="Approve or reject a pending Hermes human gate",
    )
