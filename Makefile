.PHONY: all test package-check coverage reproducibility-check wheel source-dist distributions rpm-source rpm-binary rpm-check checksums release wheel-smoke benchmark run daemon docker-build docker-run install

PYTHON ?= python3
DIST_DIR ?= dist
export DIST_DIR
PROJECT_ROOT := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
# Keep release filenames tied to the single version declaration in pyproject.toml.
# This also works with the project's Python 3.9 minimum, where tomllib is not
# part of the standard library.
VERSION := $(shell sed -n 's/^[[:space:]]*version[[:space:]]*=[[:space:]]*"\([^"[:space:]]*\)".*/\1/p' "$(PROJECT_ROOT)pyproject.toml" | sed -n '1p')
ifeq ($(strip $(VERSION)),)
$(error Could not determine project version from pyproject.toml)
endif
# Give Python archive builders a stable timestamp for the current source
# revision, while allowing release tooling to supply its own value.
SOURCE_DATE_EPOCH ?= $(shell git -C "$(PROJECT_ROOT)" log -1 --format=%ct 2>/dev/null || printf '0')
export SOURCE_DATE_EPOCH
RPM_RELEASE := 1.fc44
WHEEL_FILE := cooler_btop-$(VERSION)-py3-none-any.whl
SDIST_FILE := cooler_btop-$(VERSION).tar.gz
SRPM_FILE := cooler-btop-$(VERSION)-$(RPM_RELEASE).src.rpm
RPM_FILE := cooler-btop-$(VERSION)-$(RPM_RELEASE).noarch.rpm
RELEASE_FILES := $(RPM_FILE) $(SRPM_FILE) $(WHEEL_FILE) $(SDIST_FILE) SHA256SUMS

all: test

install:
	./install.sh

test:
	$(PYTHON) -m unittest discover -v

package-check:
	$(PYTHON) -m unittest -v test_packaging

coverage:
	$(PYTHON) -m coverage run --branch --source=cooler_btop -m unittest discover -v
	$(PYTHON) -m coverage report --fail-under=70

reproducibility-check:
	@set -eu; \
	first_dir=$$(mktemp -d); \
	second_dir=$$(mktemp -d); \
	trap 'rm -rf -- "$$first_dir" "$$second_dir"' EXIT HUP INT TERM; \
	$(MAKE) --no-print-directory -j1 DIST_DIR="$$first_dir" PYTHON="$(PYTHON)" wheel source-dist; \
	$(MAKE) --no-print-directory -j1 DIST_DIR="$$second_dir" PYTHON="$(PYTHON)" wheel source-dist; \
	cmp -- "$$first_dir/$(WHEEL_FILE)" "$$second_dir/$(WHEEL_FILE)"; \
	cmp -- "$$first_dir/$(SDIST_FILE)" "$$second_dir/$(SDIST_FILE)"

wheel:
	mkdir -p -- "$${DIST_DIR}"
	$(PYTHON) -m pip wheel --no-deps --wheel-dir "$${DIST_DIR}" .

source-dist:
	mkdir -p -- "$${DIST_DIR}"
	$(PYTHON) -c 'import os, setuptools.build_meta as backend; backend.build_sdist(os.environ["DIST_DIR"])'
	@set -eu -o pipefail; \
	sdist="$${DIST_DIR}/$(SDIST_FILE)"; \
	archive_dir=$$(mktemp -d); \
	archive_tar=$$(mktemp); \
	archive_gz=$$(mktemp); \
	trap 'rm -rf -- "$$archive_dir"; rm -f -- "$$archive_tar" "$$archive_gz"' EXIT HUP INT TERM; \
	archive_root=$$(tar -tzf "$$sdist" | sed -n '1s:/$$::p'); \
	test -n "$$archive_root"; \
	tar -xzf "$$sdist" -C "$$archive_dir"; \
	tar --sort=name --owner=0 --group=0 --numeric-owner \
		--mtime="@$$SOURCE_DATE_EPOCH" -C "$$archive_dir" \
		-cf "$$archive_tar" "$$archive_root"; \
	gzip -n -c "$$archive_tar" > "$$archive_gz"; \
	mv -- "$$archive_gz" "$$sdist"; \
	trap - EXIT HUP INT TERM; \
	rm -rf -- "$$archive_dir"; \
	rm -f -- "$$archive_tar"

distributions: wheel source-dist

rpm-source: source-dist
	@command -v rpmbuild >/dev/null 2>&1 || { \
		printf 'rpmbuild is not installed; source distribution is available in %s.\n' "$${DIST_DIR}"; \
		exit 1; \
	}
	@set -eu; \
	dist_dir=$$(realpath -- "$${DIST_DIR}"); \
	rpmbuild -bs packaging/cooler-btop.spec \
		--define "_sourcedir $$dist_dir" \
		--define "_srcrpmdir $$dist_dir"

rpm-binary: source-dist
	@set -eu; \
	dist_dir=$$(realpath -- "$${DIST_DIR}"); \
	rpmbuild -bb packaging/cooler-btop.spec \
		--define "_sourcedir $$dist_dir" \
		--define "_rpmdir $$dist_dir" \
		--define "_rpmfilename %%{NAME}-%%{VERSION}-%%{RELEASE}.%%{ARCH}.rpm"

rpm-check:
	packaging/verify-rpm.sh "$${DIST_DIR}/$(RPM_FILE)"

checksums:
	@set -eu; \
	dist_dir=$$(realpath -- "$${DIST_DIR}"); \
	tmp_manifest=$$(mktemp "$$dist_dir/SHA256SUMS.XXXXXX"); \
	trap 'rm -f -- "$$tmp_manifest"' EXIT HUP INT TERM; \
	( cd "$$dist_dir" && sha256sum "$(RPM_FILE)" "$(SRPM_FILE)" \
		"$(WHEEL_FILE)" "$(SDIST_FILE)" > "$$tmp_manifest" ); \
	mv -- "$$tmp_manifest" "$$dist_dir/SHA256SUMS"; \
	trap - EXIT HUP INT TERM; \
	cd "$$dist_dir" && sha256sum --check SHA256SUMS

release:
	@set -eu; \
	final_dir=$$(realpath -m -- "$${DIST_DIR}"); \
	if [ -e "$$final_dir" ] && [ ! -d "$$final_dir" ]; then \
		printf '%s\n' "release DIST_DIR is not a directory: $$final_dir" >&2; \
		exit 1; \
	fi; \
	mkdir -p -- "$$final_dir"; \
	validate_entries() { \
		directory=$$1; \
		require_all=$$2; \
		label=$$3; \
		for entry in "$$directory"/* "$$directory"/.[!.]* "$$directory"/..?*; do \
			if [ ! -e "$$entry" ] && [ ! -L "$$entry" ]; then continue; fi; \
			name=$${entry##*/}; \
			case "$$name" in \
				$(RPM_FILE)|$(SRPM_FILE)|$(WHEEL_FILE)|$(SDIST_FILE)|SHA256SUMS) ;; \
				*) printf '%s\n' "release found unrelated entry in $$label: $$name" >&2; return 1 ;; \
			esac; \
			if [ ! -f "$$entry" ] || [ -L "$$entry" ]; then \
				printf '%s\n' "release entry is not a regular file in $$label: $$name" >&2; \
				return 1; \
			fi; \
		done; \
		if [ "$$require_all" = yes ]; then \
			for name in $(RELEASE_FILES); do \
				if [ ! -f "$$directory/$$name" ] || [ -L "$$directory/$$name" ]; then \
					printf '%s\n' "release output missing from $$label: $$name" >&2; \
					return 1; \
				fi; \
			done; \
		fi; \
	}; \
	validate_entries "$$final_dir" no "DIST_DIR"; \
	stage_dir=$$(mktemp -d); \
	trap 'rm -rf "$$stage_dir"' EXIT HUP INT TERM; \
	$(MAKE) --no-print-directory -j1 DIST_DIR="$$stage_dir" \
		test distributions wheel-smoke rpm-source rpm-binary rpm-check checksums; \
	validate_entries "$$stage_dir" yes "staging directory"; \
	for name in $(RELEASE_FILES); do \
		cp -- "$$stage_dir/$$name" "$$final_dir/$$name"; \
	done; \
	validate_entries "$$final_dir" yes "DIST_DIR"

wheel-smoke: wheel
	@set -eu; \
	tmpdir=$$(mktemp -d); \
	trap 'rm -rf "$$tmpdir"' EXIT; \
	wheel="$${DIST_DIR}/$(WHEEL_FILE)"; \
	test -f "$$wheel"; \
	wheel=$$(realpath -- "$$wheel"); \
	$(PYTHON) -m venv "$$tmpdir/venv"; \
	cd "$$tmpdir"; \
	"$$tmpdir/venv/bin/python" -m pip install -r "$(PROJECT_ROOT)requirements.lock"; \
	"$$tmpdir/venv/bin/python" -m pip install --no-deps "$$wheel"; \
	"$$tmpdir/venv/bin/cooler-btop" --version

benchmark:
	$(PYTHON) benchmark.py

run:
	$(PYTHON) -m cooler_btop

daemon:
	$(PYTHON) -m cooler_btop --daemon --port 8080 --log-db metrics.sqlite

docker-build:
	docker compose build

docker-run:
	docker compose up -d
