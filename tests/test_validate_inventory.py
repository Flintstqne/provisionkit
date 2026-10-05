import shutil, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from validate_inventory import validate

EX = Path(__file__).parent.parent / "inventories/example"


def mutated(old, new, f):
    d = Path(tempfile.mkdtemp()) / "inv"
    shutil.copytree(EX, d)
    p = d / f
    p.write_text(p.read_text().replace(old, new))
    return d


assert validate(EX, example=True) == []
assert any("documentation" in e for e in validate(EX))
assert any("empty" in e for e in validate(mutated("provisionkit_management_sources:\n  - 192.0.2.10/32  # controller (example)\n  - 192.0.2.40/32  # admin workstation (example)", "provisionkit_management_sources: []", "group_vars/all.yml"), example=True))
assert any("0.0.0.0/0" in e for e in validate(mutated("192.0.2.40/32", "0.0.0.0/0", "group_vars/all.yml"), example=True))
assert any("k3s" in e for e in validate(mutated("pk-worker: {}", "pk-control: {}", "hosts.yml"), example=True))
print("ok")
