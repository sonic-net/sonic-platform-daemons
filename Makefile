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

.PHONY: all build test test-coverage clean install lint format check vendor-link \
        ci-all ci-format ci-lint ci-build ci-doc ci-test

# Every .rs file this repository owns.  Used by the targets that must work in a
# bare checkout, where cargo cannot be used at all (see ci-format).
RUST_SOURCES = $(shell find crates -name '*.rs')

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

# Format the source.
#
# rustfmt rather than `cargo fmt`: cargo has to resolve every dependency in the
# manifest before it will format anything, and three of them - platform-traits,
# swss-common and the vendor's platform API - live outside this repository.  In
# a bare checkout `cargo fmt` therefore fails before it formats a single line.
# rustfmt only parses, so it works anywhere.  The result is identical: both read
# rustfmt.toml at the repository root.
format:
	rustfmt --edition 2021 $(RUST_SOURCES)

# Verify without rewriting.
check: ci-format lint

#
# CI targets.  Named to match sonic-dash-ha's, which is the only other Rust in
# SONiC, so that a contributor moving between the two repositories runs the same
# commands.
#

ci-all: ci-format ci-lint ci-build ci-doc ci-test

# Formatting, and the one check that needs nothing outside this repository: no
# vendor-platform symlink, no sibling checkouts, no libswsscommon.  That is why
# it is rustfmt and not `cargo fmt --check` (see `format` above).
ci-format:
	rustfmt --edition 2021 --check $(RUST_SOURCES)

# clippy::all denied, not warned.
#
# Unlike ci-format this one compiles, so it needs the three path dependencies
# resolved: VENDOR_PLATFORM_RS_PATH for the symlink, and sonic-platform-common
# and sonic-swss-common checked out beside this repository.  -p rather than
# --workspace on purpose: the vendor-platform symlink resolves inside the
# workspace directory, so cargo counts the vendor's crate as a member and
# --workspace would lint the vendor's code from here.
ci-lint: vendor-link
	cargo clippy --locked -p sonic-thermalctld-rs --all-targets --all-features --no-deps -- --deny "clippy::all"

# Warnings are errors.  Debug and release both, because a cfg or a lint can
# differ between the two and the shipped binary is the release one -- and this
# workspace's release profile is not cosmetic: lto, one codegen unit and
# panic = "abort", the last of which the hw-management-tc panic hook depends on.
#
# sonic-dash-ha runs `cargo clean` between the two passes; that is a disk-space
# measure for its agent pool, and it would throw away a contributor's build
# cache every time they ran this.  Omitted: the two use separate target
# directories, so they do not collide.
#
# Like ci-lint, these compile, so they need the vendor symlink and the two
# sibling checkouts.  Only ci-format runs without them.
ci-build: vendor-link
	RUSTFLAGS="--deny warnings" cargo build --locked -p sonic-thermalctld-rs --all-features
	RUSTFLAGS="--deny warnings" cargo build --locked -p sonic-thermalctld-rs --all-features --release

# A broken intra-doc link is a broken link whether or not anything reads it.
ci-doc: vendor-link
	RUSTDOCFLAGS="--deny warnings" cargo doc --locked -p sonic-thermalctld-rs --all-features --no-deps
	RUSTDOCFLAGS="--deny warnings" cargo doc --locked -p sonic-thermalctld-rs --all-features --no-deps --release

# cargo ignores the workspace's panic = "abort" for the test profile, so the
# release pass here still unwinds and the suite behaves the same in both.
ci-test: vendor-link
	cargo test --locked -p sonic-thermalctld-rs --all-features
	cargo test --locked -p sonic-thermalctld-rs --all-features --release
