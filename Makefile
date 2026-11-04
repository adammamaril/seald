PREFIX ?= $(HOME)/.local
BINDIR ?= $(PREFIX)/bin

.PHONY: deps install uninstall test

deps:
	./scripts/install-deps.sh

install:
	install -d "$(BINDIR)"
	install -m 0755 seal "$(BINDIR)/seal"

uninstall:
	rm -f "$(BINDIR)/seal"

test:
	python3 -m unittest discover -s tests -v
