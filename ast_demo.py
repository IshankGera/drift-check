import ast

# A tiny fake codebase
code = """
from openai import OpenAI
client = OpenAI()
client.chat.completions.create(
    model="gpt-4",
    temperature=0.2
)
"""

# Parse the code into an AST
tree = ast.parse(code)

# Print the tree nicely formatted
print(ast.dump(tree, indent=2))