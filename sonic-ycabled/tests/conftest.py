import sys
import os

def pytest_configure(config):
    print("\n=== DEBUG: sys.path ===")
    for p in sys.path:
        print(f"  {p}")
    print("\n=== DEBUG: PYTHONPATH ===")
    print(f"  {os.environ.get('PYTHONPATH', 'NOT SET')}")
    print(f"  HOME={os.environ.get('HOME')}")
    print(f"  USER={os.environ.get('USER')}")
    print(f"  VIRTUAL_ENV={os.environ.get('VIRTUAL_ENV', 'NOT SET')}")
    print("\n=== DEBUG: google namespace ===")
    try:
        import google
        print(f"  google.__path__: {google.__path__}")
        print(f"  google.__file__: {getattr(google, '__file__', 'NONE')}")
        import google.protobuf
        print(f"  protobuf version: {google.protobuf.__version__}")
        print(f"  protobuf file: {google.protobuf.__file__}")
    except Exception as e:
        print(f"  FAILED: {e}")
    print("\n=== DEBUG: pip list google/grpc ===")
    os.system("pip3 list 2>/dev/null | grep -iE 'protobuf|grpcio|google'")
    print("\n=== DEBUG: find google dirs ===")
    for base in ['/usr/local/lib', '/usr/lib/python3', os.path.expanduser('~/.local/lib')]:
        for root, dirs, files in os.walk(base):
            if 'google/protobuf' in root and '__init__.py' in files:
                print(f"  {os.path.join(root, '__init__.py')}")
                break
    print("=== END DEBUG ===\n")
