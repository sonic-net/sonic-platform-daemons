# Makefile for sonic-thermalctld-rs
#
# Every cargo target needs the `vendor-platform` symlink, because Cargo.toml
# depends on `sonic-platform` through it.  A fresh checkout has no symlink, so
# each target below creates it first, the same way debian/rules does for the
# package build.  VENDOR_PLATFORM_RS_PATH comes from platform/<vendor>/rules.mk
# in a SONiC build; set it by hand to work on this crate on its own:
#
#   make VENDOR_PLATFORM_RS_PATH=$(realpath ../../platform/mellanox/mlnx-platform-api-rs)
#
VENDOR_PLATFORM_LINK = vendor-platform

.PHONY: all build test test-coverage clean install lint format check vendor-link

# Default target
all: build test

# Point the fixed crate name at the vendor's directory.
vendor-link:
	@test -n "$(VENDOR_PLATFORM_RS_PATH)" || { \
	  echo "VENDOR_PLATFORM_RS_PATH is unset; set it to the vendor platform API directory"; \
	  exit 1; }
	ln -sfn $(VENDOR_PLATFORM_RS_PATH) $(VENDOR_PLATFORM_LINK)

# Build the project
build: vendor-link
	cargo build --locked --release

# Run tests
test: vendor-link
	cargo test --locked

# Run tests with coverage.
#
# Uses the tarpaulin the slave image already carries rather than installing one:
# sonic-slave-bookworm pins cargo-tarpaulin@0.35.1 deliberately, because 0.35.2
# pulls in a gimli that wants Rust 1.88 while the image is on 1.86
# (sonic-slave-bookworm/Dockerfile.j2:804-806).  Installing here would resolve
# to the newest version and undo that.
test-coverage: vendor-link
	cargo tarpaulin --locked --out Html --output-dir target/coverage --timeout 120 --all-features

# Clean build artifacts
clean:
	cargo clean
	rm -f $(VENDOR_PLATFORM_LINK)

# Install the binary
install: build
	cargo install --locked --path .

# Run linting
lint: vendor-link
	cargo clippy --locked --all-targets --all-features -- -D warnings

# Format the source
format:
	cargo fmt

# Verify lints without rewriting.
#
# Deliberately not `cargo fmt -- --check`: these crates align columns by hand in
# places rustfmt collapses (the constant blocks, the struct literals), so the
# check would fail on every file and enforce nothing anyone acts on.  `format`
# stays available for whoever wants it.
check: lint
