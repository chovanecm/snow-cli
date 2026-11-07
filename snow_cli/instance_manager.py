"""Instance management commands"""
import sys
import getpass
from .config import Config
from .instance_repository import InstanceRepository


def add_instance(instance: str = None, set_default: bool = False):
    """Add a new ServiceNow instance"""
    try:
        # Prompt for instance if not provided
        if not instance:
            instance = input("ServiceNow instance (e.g. dev1234.service-now.com): ").strip()

        if not instance:
            print("Error: Instance is required", file=sys.stderr)
            return 1

        # Prompt for credentials
        user = input(f"Username for {instance}: ").strip()
        if not user:
            print("Error: Username is required", file=sys.stderr)
            return 1

        password = getpass.getpass(f"Password for {user}@{instance}: ")
        if not password:
            print("Error: Password is required", file=sys.stderr)
            return 1

        # Save to config
        repo = InstanceRepository()
        keyring_success = repo.save(instance, user, password, set_default)

        print(f"\n✓ Added instance: {instance}")
        print(f"  User: {user}")

        if keyring_success:
            print(f"  Password: Stored in system keyring (secure)")
        else:
            print(f"  Password: Stored in config file (fallback)")
            print(f"  Warning: System keyring not available, using less secure storage")

        if set_default or repo.get_default() == instance:
            print(f"  Set as default instance")

        return 0

    except KeyboardInterrupt:
        print("\n\nCancelled.", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error adding instance: {e}", file=sys.stderr)
        return 1


def add_oauth_instance(instance: str = None, client_id: str = None, set_default: bool = False):
    """Add a ServiceNow instance that signs in through OAuth"""
    try:
        if not instance:
            instance = input("ServiceNow instance (e.g. dev1234.service-now.com): ").strip()
        Config(instance=instance).ensure_instance_set()

        client_id = client_id or input(f"OAuth client ID for {instance}: ").strip()
        if not client_id:
            print("Error: Client ID is required", file=sys.stderr)
            return 1

        client_secret = getpass.getpass(f"OAuth client secret for {instance}: ")
        if not client_secret:
            print("Error: Client secret is required", file=sys.stderr)
            return 1

        repo = InstanceRepository()
        keyring_success = repo.save_oauth(instance, client_id, client_secret, set_default)

        print(f"\n✓ Added instance: {instance}")
        print(f"  Client ID: {client_id}")
        if keyring_success:
            print("  Client secret: Stored in system keyring (secure)")
        else:
            print("  Client secret: Stored in config file (fallback)")
            print("  Warning: System keyring not available, using less secure storage")
        if set_default or repo.get_default() == instance:
            print("  Set as default instance")
        print(f"\nNext: run 'snow -i {instance} login' to sign in through the browser.")
        return 0

    except KeyboardInterrupt:
        print("\n\nCancelled.", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error adding instance: {e}", file=sys.stderr)
        return 1


def list_instances():
    """List all configured instances"""
    repo = InstanceRepository()
    instances = repo.list_all()
    default = repo.get_default()

    if not instances:
        print("No instances configured.")
        print("\nRun 'snow add' to add an instance.")
        return 0

    print("Configured instances:")
    print()
    for instance, creds in instances.items():
        marker = " (default)" if instance == default else ""
        keyring_marker = " [keyring]" if creds.get("keyring") else " [config file]"
        print(f"  {instance}{marker}")
        if creds.get("auth") == "oauth":
            print("    Auth: OAuth authorization code")
            print(f"    Client ID: {creds.get('client_id')}")
            print(f"    Client secret: Stored in{keyring_marker}")
        else:
            print(f"    User: {creds['user']}")
            print(f"    Password: Stored in{keyring_marker}")
        print()

    return 0


def use_instance(instance: str):
    """Set default instance"""
    try:
        repo = InstanceRepository()
        repo.set_default(instance)
        print(f"✓ Set default instance to: {instance}")
        return 0
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error setting default instance: {e}", file=sys.stderr)
        return 1


def remove_instance(instance: str):
    """Remove an instance"""
    try:
        repo = InstanceRepository()

        # Check if instance exists
        instances = repo.list_all()
        if instance not in instances:
            print(f"Error: Instance {instance} not found", file=sys.stderr)
            return 1

        # Confirm removal
        response = input(f"Remove instance {instance}? [y/N]: ").strip().lower()
        if response not in ("y", "yes"):
            print("Cancelled.")
            return 0

        repo.remove(instance)
        print(f"✓ Removed instance: {instance}")
        return 0

    except Exception as e:
        print(f"Error removing instance: {e}", file=sys.stderr)
        return 1


def show_info():
    """Show current configuration"""
    repo = InstanceRepository()
    config = repo.load_config()

    if config.instance:
        print(f"Current instance: {config.instance}")
        if config.is_oauth:
            print("  Auth: OAuth authorization code")
            print(f"  Client ID: {config.client_id or '(not set)'}")
            print(f"  Client secret: {'(set)' if config.client_secret else '(not set)'}")
        else:
            print(f"  User: {config.user or '(not set)'}")
            print(f"  Password: {'(set)' if config.password else '(not set)'}")
        print(f"  Cookie file: {config.cookie_file}")
    else:
        default = repo.get_default()
        if default:
            print(f"Default instance: {default}")
        else:
            print("No instance configured.")

    print()

    instances = repo.list_all()
    if instances:
        print(f"Total instances configured: {len(instances)}")
        print("\nRun 'snow list' to see all instances.")
    else:
        print("No instances configured.")
        print("Run 'snow add' to add an instance.")

    return 0
