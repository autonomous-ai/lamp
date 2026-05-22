# Lumi Buddy — macOS build/run shortcuts.
# Run from this directory: `make <target>`. `make help` for the list.

APP_NAME   := LumiBuddy
BUNDLE_ID  := network.autonomous.ai.lumi-buddy
VERSION    := 0.0.1

MACOS_DIR  := macos
DIST_DIR   := dist
INFO_PLIST := $(MACOS_DIR)/Info.plist
BUILD_DIR  := $(MACOS_DIR)/.build
RELEASE_BIN := $(BUILD_DIR)/release/$(APP_NAME)
APP_BUNDLE := $(DIST_DIR)/$(APP_NAME).app
DMG_FILE   := $(DIST_DIR)/$(APP_NAME)-$(VERSION).dmg
DMG_STAGE  := $(DIST_DIR)/dmg-stage
DMG_VOL    := $(APP_NAME) $(VERSION)
ICON_SCRIPT := $(MACOS_DIR)/scripts/gen-app-icon.swift
ICON_MASTER := $(DIST_DIR)/AppIcon-master.png
ICONSET_DIR := $(DIST_DIR)/AppIcon.iconset
ICNS_FILE  := $(DIST_DIR)/AppIcon.icns
AUDIT_LOG  := $(HOME)/Library/Application Support/LumiBuddy/audit.log

MOCK_DIR := mock-lamp

.PHONY: help run build release app install open clean audit kill all check mock mock-build dmg icon

.DEFAULT_GOAL := help

help: ## Show this help
	@echo "Lumi Buddy — make targets:"
	@echo ""
	@awk 'BEGIN {FS = ":.*##"} /^[a-zA-Z_-]+:.*##/ {printf "  \033[1m%-12s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@echo ""
	@echo "Typical flow:"
	@echo "  make run            # dev — runs in foreground (Ctrl-C to stop)"
	@echo "  make app && make open"
	@echo ""
	@echo "Right-click → Open is needed the first time (no code signing yet)."

run: ## Run buddy in dev mode (foreground)
	cd $(MACOS_DIR) && swift run

build: ## Build debug binary
	cd $(MACOS_DIR) && swift build

release: ## Build release binary at macos/.build/release/LumiBuddy
	cd $(MACOS_DIR) && swift build -c release

check: ## Compile-only check (no link)
	cd $(MACOS_DIR) && swift build --build-tests

app: release $(ICNS_FILE) ## Bundle into dist/LumiBuddy.app (ad-hoc signed with bundle ID)
	@echo "==> Bundling $(APP_BUNDLE)"
	@rm -rf "$(APP_BUNDLE)"
	@mkdir -p "$(APP_BUNDLE)/Contents/MacOS"
	@mkdir -p "$(APP_BUNDLE)/Contents/Resources"
	@cp "$(RELEASE_BIN)" "$(APP_BUNDLE)/Contents/MacOS/$(APP_NAME)"
	@cp "$(INFO_PLIST)" "$(APP_BUNDLE)/Contents/Info.plist"
	@cp "$(ICNS_FILE)" "$(APP_BUNDLE)/Contents/Resources/AppIcon.icns"
	@chmod +x "$(APP_BUNDLE)/Contents/MacOS/$(APP_NAME)"
	@codesign --force --sign - --identifier "$(BUNDLE_ID)" \
		"$(APP_BUNDLE)/Contents/MacOS/$(APP_NAME)" 2>&1 | sed 's/^/    /'
	@echo "==> Done: $(APP_BUNDLE)"
	@echo "    First launch: right-click → Open (Gatekeeper blocks unsigned apps)."

icon: $(ICNS_FILE) ## Regenerate AppIcon.icns from the SF Symbol placeholder

# Pipeline: Swift script → 1024 master PNG → sips fans out to all required
# iconset sizes → iconutil packs the iconset into a single .icns.
# Re-runs on demand only; the `app` target depends on $(ICNS_FILE) so a missing
# icon triggers a regen but an existing one is reused.
$(ICNS_FILE): $(ICON_SCRIPT)
	@echo "==> Generating $(ICNS_FILE) from SF Symbol placeholder"
	@mkdir -p "$(DIST_DIR)"
	@swift "$(ICON_SCRIPT)" "$(ICON_MASTER)"
	@rm -rf "$(ICONSET_DIR)"
	@mkdir -p "$(ICONSET_DIR)"
	@sips -z 16   16   "$(ICON_MASTER)" --out "$(ICONSET_DIR)/icon_16x16.png"      >/dev/null
	@sips -z 32   32   "$(ICON_MASTER)" --out "$(ICONSET_DIR)/icon_16x16@2x.png"   >/dev/null
	@sips -z 32   32   "$(ICON_MASTER)" --out "$(ICONSET_DIR)/icon_32x32.png"      >/dev/null
	@sips -z 64   64   "$(ICON_MASTER)" --out "$(ICONSET_DIR)/icon_32x32@2x.png"   >/dev/null
	@sips -z 128  128  "$(ICON_MASTER)" --out "$(ICONSET_DIR)/icon_128x128.png"    >/dev/null
	@sips -z 256  256  "$(ICON_MASTER)" --out "$(ICONSET_DIR)/icon_128x128@2x.png" >/dev/null
	@sips -z 256  256  "$(ICON_MASTER)" --out "$(ICONSET_DIR)/icon_256x256.png"    >/dev/null
	@sips -z 512  512  "$(ICON_MASTER)" --out "$(ICONSET_DIR)/icon_256x256@2x.png" >/dev/null
	@sips -z 512  512  "$(ICON_MASTER)" --out "$(ICONSET_DIR)/icon_512x512.png"    >/dev/null
	@cp "$(ICON_MASTER)" "$(ICONSET_DIR)/icon_512x512@2x.png"
	@iconutil -c icns "$(ICONSET_DIR)" -o "$(ICNS_FILE)"
	@rm -rf "$(ICONSET_DIR)"
	@echo "==> Done: $(ICNS_FILE)"

install: app ## Copy bundled .app to /Applications (replaces existing)
	@echo "==> Installing to /Applications/$(APP_NAME).app"
	@rm -rf "/Applications/$(APP_NAME).app"
	@cp -R "$(APP_BUNDLE)" "/Applications/"
	@echo "==> Installed. Right-click → Open the first time."

dmg: app ## Package the .app into a distributable DMG (drag-to-Applications layout)
	@echo "==> Packaging $(DMG_FILE)"
	@rm -f "$(DMG_FILE)"
	@rm -rf "$(DMG_STAGE)"
	@mkdir -p "$(DMG_STAGE)"
	@cp -R "$(APP_BUNDLE)" "$(DMG_STAGE)/"
	@ln -s /Applications "$(DMG_STAGE)/Applications"
	@hdiutil create -volname "$(DMG_VOL)" -srcfolder "$(DMG_STAGE)" \
		-ov -format UDZO "$(DMG_FILE)" >/dev/null
	@rm -rf "$(DMG_STAGE)"
	@echo "==> Done: $(DMG_FILE)"
	@echo "    Send this DMG to users. They mount it and drag $(APP_NAME).app onto Applications."
	@echo "    First launch on the user's Mac: right-click $(APP_NAME).app → Open (Gatekeeper warns once)."

open: ## Launch the bundled .app from dist/
	@if [ ! -d "$(APP_BUNDLE)" ]; then echo "No bundle yet — run 'make app' first."; exit 1; fi
	open "$(APP_BUNDLE)"

kill: ## Stop any running LumiBuddy process
	-@pkill -x $(APP_NAME) 2>/dev/null && echo "Stopped $(APP_NAME)" || echo "$(APP_NAME) not running"

audit: ## Tail the audit log
	@if [ ! -f "$(AUDIT_LOG)" ]; then echo "No audit log yet at $(AUDIT_LOG)"; exit 0; fi
	@tail -f "$(AUDIT_LOG)"

clean: ## Remove all build artifacts
	@echo "==> Cleaning"
	cd $(MACOS_DIR) && swift package clean 2>/dev/null || true
	rm -rf $(BUILD_DIR) $(MACOS_DIR)/.swiftpm $(DIST_DIR)

all: app ## Alias for `make app`

mock: ## Run the mock lamp server (Go) — test buddy without real lumi side
	cd $(MOCK_DIR) && go run .

mock-build: ## Compile-only check for mock-lamp
	cd $(MOCK_DIR) && go vet ./... && go build ./...
