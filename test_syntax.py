#!/usr/bin/env python3
import sys
sys.path.insert(0, '.')
try:
    from src import model_router
    print("✓ Syntax OK - model_router imported successfully")
except SyntaxError as e:
    print(f"✗ Syntax Error: {e}")
    import traceback
    traceback.print_exc()
except Exception as e:
    print(f"✓ Syntax OK (other error: {type(e).__name__}: {e})")
