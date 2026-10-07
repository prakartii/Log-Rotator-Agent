"""Controlled OS-level tools used by the agent.

Each module wraps one step of safe log rotation and returns plain
dictionaries, so results can be passed straight to the master agent as JSON.
The agent/LLM layer never touches files directly; it only calls these tools.
"""
