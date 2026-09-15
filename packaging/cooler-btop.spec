Name:           cooler-btop
Version:        2.0.0
Release:        1%{?dist}
Summary:        Linux system monitor with terminal and local web interfaces

License:        MIT
URL:            https://github.com/l0ee/cooler-btop
Source0:        cooler_btop-%{version}.tar.gz

BuildArch:      noarch
BuildRequires:  python3-devel
BuildRequires:  python3-pip
BuildRequires:  python3-pyyaml
BuildRequires:  pyproject-rpm-macros
BuildRequires:  desktop-file-utils
BuildRequires:  appstream

%description
Cooler btop displays Linux CPU, memory, storage, network, GPU, and process
telemetry in a terminal. An explicitly started local web mode is also
available.

%prep
%autosetup -n cooler_btop-%{version}

%generate_buildrequires
%pyproject_buildrequires -r

%build
%pyproject_wheel

%install
%pyproject_install
%pyproject_save_files -l cooler_btop
install -Dpm 0644 packaging/io.github.l0ee.cooler_btop.desktop \
    %{buildroot}%{_datadir}/applications/io.github.l0ee.cooler_btop.desktop
install -Dpm 0644 packaging/io.github.l0ee.cooler_btop.svg \
    %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/io.github.l0ee.cooler_btop.svg
install -Dpm 0644 packaging/io.github.l0ee.cooler_btop.metainfo.xml \
    %{buildroot}%{_metainfodir}/io.github.l0ee.cooler_btop.metainfo.xml
install -Dpm 0644 packaging/cooler-btop.1 \
    %{buildroot}%{_mandir}/man1/cooler-btop.1

%check
python3 -m unittest discover -v
%pyproject_check_import
desktop-file-validate %{buildroot}%{_datadir}/applications/io.github.l0ee.cooler_btop.desktop
appstreamcli validate --no-net \
    %{buildroot}%{_metainfodir}/io.github.l0ee.cooler_btop.metainfo.xml

%files -f %{pyproject_files}
%doc README.md CHANGELOG.md
%{_bindir}/cooler-btop
%{_datadir}/applications/io.github.l0ee.cooler_btop.desktop
%{_datadir}/icons/hicolor/scalable/apps/io.github.l0ee.cooler_btop.svg
%{_metainfodir}/io.github.l0ee.cooler_btop.metainfo.xml
%{_mandir}/man1/cooler-btop.1*

%changelog
* Sun Sep 13 2026 l0ee <l0ee@users.noreply.github.com> - 2.0.0-1
- Initial Fedora 44 and Nobara desktop package
