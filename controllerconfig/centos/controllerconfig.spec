Summary: Controller node configuration
Name: controllerconfig
Version: 1.0
Release: %{tis_patch_ver}%{?_tis_dist}
License: Apache-2.0
Group: base
Packager: Wind River <info@windriver.com>
URL: unknown
Source0: %{name}-%{version}.tar.gz

BuildRequires: python-setuptools
BuildRequires: python2-pip
BuildRequires: python2-wheel
Requires: fm-api
Requires: psmisc
Requires: sysinv
Requires: systemd
Requires: tsconfig
Requires: python-cryptography
Requires: python-iso8601
Requires: python-keyring
Requires: python-netaddr
Requires: python-netifaces
Requires: python-pyudev
Requires: python-six
Requires: python2-crypto
Requires: python2-oslo-utils
Requires: python2-pysnmp
Requires: python2-ruamel-yaml

%description
Controller node configuration

%define local_dir /usr/
%define local_bindir %{local_dir}/bin/
%define local_etc_initd /etc/init.d/
%define local_goenabledd /etc/goenabled.d/
%define local_etc_upgraded /etc/upgrade.d/
%define local_etc_systemd /etc/systemd/system/
%define pythonroot /usr/lib64/python2.7/site-packages
%define debug_package %{nil}

%prep
%setup

%build
%{__python} setup.py build
%py2_build_wheel

# TODO: NO_GLOBAL_PY_DELETE (see python-byte-compile.bbclass), put in macro/script
%install
%{__python} setup.py install --root=$RPM_BUILD_ROOT \
                             --install-lib=%{pythonroot} \
                             --prefix=/usr \
                             --install-data=/usr/share \
                             --single-version-externally-managed
mkdir -p $RPM_BUILD_ROOT/wheels
install -m 644 dist/*.whl $RPM_BUILD_ROOT/wheels/

install -d -m 755 %{buildroot}%{local_bindir}
install -p -D -m 700 scripts/openstack_update_admin_password %{buildroot}%{local_bindir}/openstack_update_admin_password
install -p -D -m 700 scripts/upgrade_swact_migration.py %{buildroot}%{local_bindir}/upgrade_swact_migration.py
install -p -D -m 755 scripts/image-backup.sh %{buildroot}%{local_bindir}/image-backup.sh
install -p -D -m 755 scripts/migrate_helm_release.py %{buildroot}%{local_bindir}/migrate_helm_release.py

install -d -m 755 %{buildroot}%{local_goenabledd}
install -p -D -m 700 scripts/config_goenabled_check.sh %{buildroot}%{local_goenabledd}/config_goenabled_check.sh

install -d -m 755 %{buildroot}%{local_etc_initd}
install -p -D -m 755 scripts/controller_config %{buildroot}%{local_etc_initd}/controller_config

# Install Upgrade scripts
install -d -m 755 %{buildroot}%{local_etc_upgraded}
install -p -D -m 755 upgrade-scripts/* %{buildroot}%{local_etc_upgraded}/

install -d -m 755 %{buildroot}%{local_etc_systemd}
install -p -D -m 664 scripts/controllerconfig.service %{buildroot}%{local_etc_systemd}/controllerconfig.service

%post
systemctl enable controllerconfig.service

%clean
rm -rf $RPM_BUILD_ROOT

%files
%defattr(-,root,root,-)
%doc LICENSE
%{local_bindir}/*
%dir %{pythonroot}/%{name}
%{pythonroot}/%{name}/*
%dir %{pythonroot}/%{name}-%{version}.0-py2.7.egg-info
%{pythonroot}/%{name}-%{version}.0-py2.7.egg-info/*
%{local_goenabledd}/*
%{local_etc_initd}/*
%dir %{local_etc_upgraded}
%{local_etc_upgraded}/*
%{local_etc_systemd}/*

%package wheels
Summary: %{name} wheels

%description wheels
Contains python wheels for %{name}

%files wheels
/wheels/*
