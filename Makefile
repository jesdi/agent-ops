.PHONY: test vendor-skills

test:  ## Dispatcher/console unit suite
	python -m pytest -q

vendor-skills:  ## Refresh provision/claude-home/skills from provision/skills-pins.json; review the diff, then commit
	bash provision/vendor-skills.sh
