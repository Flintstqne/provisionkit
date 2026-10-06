check:
	ansible-playbook --syntax-check playbooks/preflight.yml
	ansible-playbook --syntax-check playbooks/bootstrap.yml
	ansible-playbook --syntax-check playbooks/baseline.yml
	ansible-playbook --syntax-check playbooks/validate.yml
	ansible-playbook --syntax-check playbooks/collect.yml
	ansible-playbook --syntax-check playbooks/reboot_rolling.yml
	ansible-playbook --syntax-check playbooks/llm.yml
	ansible-playbook --syntax-check playbooks/llm_bench.yml
	ansible-playbook --syntax-check tests/verify_audit.yml
	yamllint .
	python3 tests/test_validate_inventory.py
	python3 scripts/validate_inventory.py inventories/example --example
	python3 -m pytest -q tests/test_panel.py tests/test_panel_proxy.py tests/test_setup.py tests/test_scheduler.py tests/test_drift.py tests/test_baseline.py tests/test_nightly_reboot.py tests/test_nightly_cli.py tests/test_nightly_panel.py tests/test_update_cli.py tests/test_update_panel.py tests/test_config_display.py tests/test_operations.py tests/test_backup.py tests/test_backup_cli.py tests/test_install.py tests/test_pin_llm.py tests/test_cli.py

panel-demo:
	bash scripts/demo.sh

install:
	bash scripts/install.sh
