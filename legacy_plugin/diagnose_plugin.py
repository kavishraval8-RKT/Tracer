"""Diagnostic script to check KiCad 10 plugin installation.

Run this to verify why KiCad isn't discovering the plugin.
"""

import json
import os
import sys
from pathlib import Path

def check_plugin_structure(plugin_dir: Path) -> dict:
    """Check if plugin directory has valid structure."""
    results = {
        "path": str(plugin_dir),
        "exists": plugin_dir.exists(),
        "files": {},
        "plugin_json_valid": False,
        "has_entrypoint": False
    }
    
    if not plugin_dir.exists():
        return results
    
    # Check required files
    required_files = ["plugin.json", "__init__.py", "requirements.txt"]
    for filename in required_files:
        file_path = plugin_dir / filename
        results["files"][filename] = file_path.exists()
    
    # Validate plugin.json
    plugin_json = plugin_dir / "plugin.json"
    if plugin_json.exists():
        try:
            data = json.loads(plugin_json.read_text(encoding="utf-8"))
            results["plugin_json_valid"] = True
            results["plugin_json_data"] = data
            
            # Check for entrypoint
            if "actions" in data and len(data["actions"]) > 0:
                entrypoint = data["actions"][0].get("entrypoint", "")
                entrypoint_path = plugin_dir / entrypoint
                results["has_entrypoint"] = entrypoint_path.exists()
                results["entrypoint"] = entrypoint
        except Exception as e:
            results["plugin_json_error"] = str(e)
    
    return results

def main():
    print("=" * 70)
    print("KICAD 10 PLUGIN DIAGNOSTIC")
    print("=" * 70)
    
    # Check environment
    kicad_docs = os.environ.get("KICAD_DOCUMENTS_HOME", "NOT SET")
    print(f"\n📁 KICAD_DOCUMENTS_HOME: {kicad_docs}")
    
    # Check all possible plugin locations
    locations = [
        Path(r"C:\KiCad\KiCad\10.0\scripting\plugins\plugins\tracer_plugin"),
        Path(r"C:\KiCad\10.0\plugins\tracer_plugin"),
        Path.home() / "Documents" / "KiCad" / "10.0" / "plugins" / "tracer_plugin",
    ]
    
    if kicad_docs != "NOT SET":
        locations.insert(0, Path(kicad_docs) / "10.0" / "plugins" / "tracer_plugin")
    
    print(f"\n🔍 Checking {len(locations)} possible plugin locations:\n")
    
    valid_installs = []
    for location in locations:
        result = check_plugin_structure(location)
        status = "✅" if result["exists"] and result["plugin_json_valid"] else "❌"
        print(f"{status} {result['path']}")
        
        if result["exists"]:
            if result["plugin_json_valid"]:
                print(f"   ✓ Valid plugin.json")
                valid_installs.append(location)
            else:
                print(f"   ✗ Invalid or missing plugin.json")
            
            if result["has_entrypoint"]:
                print(f"   ✓ Entrypoint exists: {result['entrypoint']}")
            elif "entrypoint" in result:
                print(f"   ✗ Entrypoint missing: {result['entrypoint']}")
            
            # List files
            files = list(location.glob("*"))
            print(f"   📄 Files: {', '.join(f.name for f in files if f.is_file())}")
        print()
    
    # Check KiCad Python
    print("\n🐍 KiCad Python Environment:")
    kicad_python = Path(r"C:\Program Files\KiCad\10.0\bin\python.exe")
    if kicad_python.exists():
        print(f"   ✓ Found: {kicad_python}")
        print(f"   Python: {sys.version}")
        
        # Check if required packages are available
        try:
            import requests
            print(f"   ✓ requests: {requests.__version__}")
        except ImportError:
            print(f"   ✗ requests: NOT INSTALLED")
    else:
        print(f"   ✗ KiCad Python not found at {kicad_python}")
    
    # Summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    
    if len(valid_installs) == 0:
        print("❌ No valid plugin installations found!")
        print("\nRun: python deploy.py")
    elif len(valid_installs) == 1:
        print(f"✅ Found 1 valid plugin installation:")
        print(f"   {valid_installs[0]}")
        print("\n📋 Next steps:")
        print("   1. Close ALL KiCad windows completely")
        print("   2. Restart KiCad")
        print("   3. Open PCB Editor or Schematic Editor")
        print("   4. Go to Preferences > Plugins")
        print("   5. Click 'Refresh Plugins' or restart again")
        print("   6. Check if 'Tracer Copilot' appears in the list")
    else:
        print(f"⚠️  Found {len(valid_installs)} plugin installations:")
        for install in valid_installs:
            print(f"   • {install}")
        print("\n⚠️  Multiple installations may confuse KiCad!")
        print("   Run: python deploy.py (to clean up and reinstall)")
    
    print("\n💡 If plugin still doesn't appear after restart:")
    print("   • Check Windows Event Viewer for KiCad errors")
    print("   • Verify KiCad API is enabled: Preferences > Plugins > Enable KiCad API")
    print("   • Try: Tools > External Plugins > Refresh Plugins")
    print("   • Check KiCad version is 10.0 or newer")
    
    return 0

if __name__ == "__main__":
    sys.exit(main())
