"""System-level prompt templates for the agent.

In langchain 1.3+, the agent uses a graph-based (LangGraph) architecture.
The system prompt is passed directly to create_agent().
"""
# 英文提示词每行末尾或下一行开头必须加空格，否则会连词
SYSTEM_PROMPT = (
    "You are a helpful AI assistant powered by LangChain. "
    "You have access to tools that can help you answer questions. "
    "Use them when needed, and be concise and accurate in your responses."
)

# Additional system prompt variants can be defined here
TECHNICAL_ASSISTANT_PROMPT = (
    "You are a technical AI assistant powered by DeepSeek and LangChain. "
    "You specialize in programming, debugging, and technical problem-solving. "
    "Use tools to look up information when needed."
)
