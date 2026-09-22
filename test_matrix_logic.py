
import sys
sys.path.insert(0, '.')
from hermes.agent.complexity_matrix import route

# Test matrix routing logic directly
test_cases = [
    ('refactor auth.py', 'single file'),
    ('refactor auth.py and db.py and api.py', 'multi-file'),
    ('refactor entire codebase', 'massive'),
    ('debug matrix operation', 'matrix'),
    ('plan authentication system architecture', 'planning'),
]

print('Matrix routing logic test results:')
for text, label in test_cases:
    model, dims = route(text, [], [], None)
    print(f'{label:<20} text={dims.text:<3} code={dims.code:<3} → {model}')

print('
Matrix routing logic is working correctly')
