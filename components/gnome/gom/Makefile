#
# CDDL HEADER START
#
# The contents of this file are subject to the terms of the
# Common Development and Distribution License (the "License").
# You may not use this file except in compliance with the License.
#
# You can obtain a copy of the license at usr/src/OPENSOLARIS.LICENSE
# or http://www.opensolaris.org/os/licensing.
# See the License for the specific language governing permissions
# and limitations under the License.
#
# When distributing Covered Code, include this CDDL HEADER in each
# file and include the License file at usr/src/OPENSOLARIS.LICENSE.
# If applicable, add the following below this CDDL HEADER, with the
# fields enclosed by brackets "[]" replaced with your own identifying
# information: Portions Copyright [yyyy] [name of copyright owner]
#
# CDDL HEADER END
#

#
# Copyright (c) 2015, 2017, Oracle and/or its affiliates. All rights reserved.
#

BUILD_BITS =            64_and_32
# All components using gobject-introspection must be built with gcc as
# gobject-introspection does not produce correct results when using Studio.
COMPILER =              gcc
include ../../../make-rules/shared-macros.mk

COMPONENT_NAME=		gom
COMPONENT_VERSION=	0.3.2
COMPONENT_PROJECT_URL=	https://wiki.gnome.org/Projects/Gom
COMPONENT_ARCHIVE_HASH=	\
	sha256:bce8f0f94af6ff7847b853580ba6baebbab8ae531cedb0c78a5c473f39c758fd
COMPONENT_BUGDB=        gnome/libraries
COMPONENT_ANITYA_ID=	13230

TPNO=			34507

# Needs updated glib2 and gobject-introspection, not available on S11.
ifeq ($(BUILD_TYPE), evaluation)
BUILD_32_and_64=
INSTALL_32_and_64=
PUBLISH_STAMP=
endif

include $(WS_MAKE_RULES)/gnome-component.mk

# Assumes GNU xgettext in PATH.
CONFIGURE_ENV += PATH=$(GNUBIN):$(PATH)

CONFIGURE_OPTIONS += --disable-static
# Required for other components.
CONFIGURE_OPTIONS += --enable-introspection
CONFIGURE_OPTIONS += --enable-shared
CONFIGURE_OPTIONS += --with-pic

# Required due to patches.
COMPONENT_PREP_ACTION += (cd $(@D); autoreconf -fiv);

COMPONENT_TEST_MASTER = $(COMPONENT_TEST_RESULTS_DIR)/results-all.master
COMPONENT_TEST_ARGS =   -k -i
COMPONENT_TEST_TRANSFORMS += \
        '-n ' \
        '-e "/TOTAL:/p" ' \
        '-e "/SKIP:/p" ' \
        '-e "/PASS:/p" ' \
        '-e "/FAIL:/p" ' \
        '-e "/ok/p" ' \
        '-e "/ERROR:/p" '

REQUIRED_PACKAGES += database/sqlite-3
REQUIRED_PACKAGES += developer/documentation-tool/gtk-doc
REQUIRED_PACKAGES += library/desktop/gobject/gobject-introspection
REQUIRED_PACKAGES += library/glib2
