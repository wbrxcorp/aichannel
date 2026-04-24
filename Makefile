INSTALL_BIN    = $(HOME)/.local/bin/aichannel
INSTALL_DIR    = $(HOME)/.aichannel
SERVICE_DIR    = $(HOME)/.config/systemd/user
SERVICE_FILE   = $(SERVICE_DIR)/aichannel.service

.PHONY: install

install:
	install -D -m 755 aichannel.py $(INSTALL_BIN)
	mkdir -p $(INSTALL_DIR)
	# instructions.md は上書きしない（カスタマイズ済みの場合に備えて）
	test -f $(INSTALL_DIR)/instructions.md || cp instructions.md $(INSTALL_DIR)/instructions.md
	mkdir -p $(SERVICE_DIR)
	mkdir -p $(INSTALL_DIR)/git
	mkdir -p $(INSTALL_DIR)/blob
	printf '[Unit]\nDescription=AIちゃんねる\n\n[Service]\nExecStart=%%h/.local/bin/aichannel \\\n    --db %%h/.aichannel/aichannel.sqlite \\\n    --instructions %%h/.aichannel/instructions.md \\\n    --git-base %%h/.aichannel/git \\\n    --blob-dir %%h/.aichannel/blob \\\n    --socket %%t/aichannel.sock\nRestart=on-failure\n\n[Install]\nWantedBy=default.target\n' > $(SERVICE_FILE)
	systemctl --user daemon-reload
	@echo "installed."
	@echo "To enable: systemctl --user enable --now aichannel"
