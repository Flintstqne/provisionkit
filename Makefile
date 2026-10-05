check:
	ansible-playbook --syntax-check playbooks/preflight.yml
	ansible-playbook --syntax-check playbooks/bootstrap.yml
	ansible-playbook --syntax-check playbooks/baseline.yml
	ansible-playbook --syntax-check playbooks/validate.yml
	ansible-playbook --syntax-check tests/verify_audit.yml
	yamllint .
	python3 tests/test_validate_inventory.py
	python3 scripts/validate_inventory.py inventories/example --example
