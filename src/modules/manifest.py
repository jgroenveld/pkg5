#!/usr/bin/python2.4
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
# Copyright 2009 Sun Microsystems, Inc.  All rights reserved.
# Use is subject to license terms.
#

import os
import errno
from itertools import groupby, chain, repeat
from pkg.misc import EmptyI

import pkg.actions as actions
from pkg.actions.attribute import AttributeAction

# The type member is used for the ordering of actions.
ACTION_DIR = 10
ACTION_FILE = 20
ACTION_LINK = 50
ACTION_HARDLINK = 55
ACTION_DEVICE = 100
ACTION_USER = 200
ACTION_GROUP = 210
ACTION_SERVICE = 300
ACTION_RESTART = 310
ACTION_DEPEND = 400

DEPEND_REQUIRE = 0
DEPEND_OPTIONAL = 1
DEPEND_INCORPORATE = 10

depend_str = { DEPEND_REQUIRE : "require",
                DEPEND_OPTIONAL : "optional",
                DEPEND_INCORPORATE : "incorporate"
}

class Manifest(object):
        """A Manifest is the representation of the actions composing a specific
        package version on both the client and the repository.  Both purposes
        utilize the same storage format.

        The serialized structure of a manifest is an unordered list of actions.

        The special action, "set", represents a package attribute.

        The reserved attribute, "fmri", represents the package and version
        described by this manifest.  It is available as a string via the
        attributes dictionary, and as an FMRI object from the fmri member.

        The list of manifest-wide reserved attributes is

        base_directory          Default base directory, for non-user images.
        fmri                    Package FMRI.
        isa                     Package is intended for a list of ISAs.
        platform                Package is intended for a list of platforms.
        relocatable             Suitable for User Image.

        All non-prefixed attributes are reserved to the framework.  Third
        parties may prefix their attributes with a reversed domain name, domain
        name, or stock symbol.  An example might be

        com.example,supported

        as an indicator that a specific package version is supported by the
        vendor, example.com.

        manifest.null is provided as the null manifest.  Differences against the
        null manifest result in the complete set of attributes and actions of
        the non-null manifest, meaning that all operations can be viewed as
        tranitions between the manifest being installed and the manifest already
        present in the image (which may be the null manifest).
        """

        def __init__(self):
                self.img = None
                self.fmri = None

                self.size = 0
                self.actions = []
                self.actions_bytype = {}
                self.variants = {}   # variants seen in package
                self.facets = {}     # facets seen in package
                self.attributes = {} # package-wide attributes

        def __str__(self):
                r = ""
                if self.fmri != None:
                        r += "set name=fmri value=%s\n" % self.fmri

                for act in sorted(self.actions):
                        r += "%s\n" % act
                return r

        def tostr_unsorted(self):
                r = ""
                if self.fmri != None:
                        r += "set name=fmri value=%s\n" % self.fmri

                for act in self.actions:
                        r += "%s\n" % act
                return r


        def difference(self, origin, origin_exclude=EmptyI,
            self_exclude=EmptyI):
                """Return three lists of action pairs representing origin and
                destination actions.  The first list contains the pairs
                representing additions, the second list contains the pairs
                representing updates, and the third list contains the pairs
                represnting removals.  All three lists are in the order in which
                they should be executed."""
                # XXX Do we need to find some way to assert that the keys are
                # all unique?

                sdict = dict(
                    ((a.name, a.attrs.get(a.key_attr, id(a))), a)
                    for a in self.gen_actions(self_exclude)
                )
                odict = dict(
                    ((a.name, a.attrs.get(a.key_attr, id(a))), a)
                    for a in origin.gen_actions(origin_exclude)
                )

                sset = set(sdict.keys())
                oset = set(odict.keys())

                added = [(None, sdict[i]) for i in sset - oset]
                removed = [(odict[i], None) for i in oset - sset]
                # XXX for now, we force license actions to always be
                # different to insure that existing license files for
                # new versions are always installed
                changed = [
                    (odict[i], sdict[i])
                    for i in oset & sset
                    if odict[i].different(sdict[i]) or i[0] == "license"
                ]

                # XXX Do changed actions need to be sorted at all?  This is
                # likely to be the largest list, so we might save significant
                # time by not sorting.  Should we sort above?  Insert into a
                # sorted list?

                # singlesort = lambda x: x[0] or x[1]
                addsort = lambda x: x[1]
                remsort = lambda x: x[0]
                removed.sort(key = remsort, reverse = True)
                added.sort(key = addsort)
                changed.sort(key = addsort)

                return (added, changed, removed)

        @staticmethod
        def comm(*compare_m):
                """Like the unix utility comm, except that this function
                takes an arbitrary number of manifests and compares them,
                returning a tuple consisting of each manifest's actions
                that are not the same for all manifests, followed by a
                list of actions that are the same in each manifest."""

                # construct list of dictionaries of actions in each
                # manifest, indexed by unique keys
                m_dicts = [
                    dict(
                    ((a.name, a.attrs.get(a.key_attr, id(a))), a)
                    for a in m.actions)
                    for m in compare_m
                ]
                # construct list of key sets in each dict
                #
                m_sets = [
                    set(m.keys())
                    for m in m_dicts
                ]

                common_keys = reduce(lambda a, b: a & b, m_sets)

                # determine which common_keys have common actions
                for k in common_keys.copy():
                        for i in range(len(m_dicts) - 1):
                                if m_dicts[i][k].different(
                                    m_dicts[i + 1][k]):
                                        common_keys.remove(k)
                                        break
                return tuple(
                    [
                        [m_dicts[i][k] for k in m_sets[i] - common_keys]
                        for i in range(len(m_dicts))
                    ]
                    +
                    [
                        [ m_dicts[0][k] for k in common_keys ]
                    ]
                )


        def combined_difference(self, origin, ov=EmptyI, sv=EmptyI):
                """Where difference() returns three lists, combined_difference()
                returns a single list of the concatenation of the three."""
                return list(chain(*self.difference(origin, ov, sv)))

        def humanized_differences(self, other, ov=EmptyI, sv=EmptyI):
                """Output expects that self is newer than other.  Use of sets
                requires that we convert the action objects into some marshalled
                form, otherwise set member identities are derived from the
                object pointers, rather than the contents."""

                l = self.difference(other, ov, sv)
                out = ""

                for src, dest in chain(*l):
                        if not src:
                                out += "+ %s\n" % str(dest)
                        elif not dest:
                                out += "- %s\n" + str(src)
                        else:
                                out += "%s -> %s\n" % (src, dest)
                return out

        def gen_actions(self, excludes=EmptyI):
                """Generate actions in manifest through ordered callable list"""
                for a in self.actions:
                        for c in excludes:
                                if not c(a):
                                        break
                        else:
                                yield a

        def gen_actions_by_type(self, atype, excludes=EmptyI):
                """Generate actions in the manifest of type "type"
                through ordered callable list"""
                for a in self.actions_bytype.get(atype, []):
                        for c in excludes:
                                if not c(a):
                                        break
                        else:
                                yield a

        def gen_key_attribute_value_by_type(self, atype, excludes=EmptyI):
                """Generate the value of the key atrribute for each action
                of type "type" in the manifest."""

                return (
                    a.attrs.get(a.key_attr)
                    for a in self.gen_actions_by_type(atype, excludes)
                )

        def duplicates(self, excludes=EmptyI):
                """Find actions in the manifest which are duplicates (i.e.,
                represent the same object) but which are not identical (i.e.,
                have all the same attributes)."""

                def fun(a):
                        """Return a key on which actions can be sorted."""
                        return a.name, a.attrs.get(a.key_attr, id(a))

                alldups = []
                acts = [a for a in self.gen_actions(excludes)]

                for k, g in groupby(sorted(acts, key=fun), fun):
                        glist = list(g)
                        dups = set()
                        for i in range(len(glist) - 1):
                                if glist[i].different(glist[i + 1]):
                                        dups.add(glist[i])
                                        dups.add(glist[i + 1])
                        if dups:
                                alldups.append((k, dups))
                return alldups

        def set_fmri(self, img, fmri):
                self.img = img
                self.fmri = fmri

        def set_content(self, content, excludes=EmptyI):
                """content is the text representation of the manifest"""
                self.size = 0
                self.actions = []
                self.actions_bytype = {}
                self.variants = {}
                self.facets = {}
                self.attributes = {}

                # So we could build up here the type/key_attr dictionaries like
                # sdict and odict in difference() above, and have that be our
                # main datastore, rather than the simple list we have now.  If
                # we do that here, we can even assert that the "same" action
                # can't be in a manifest twice.  (The problem of having the same
                # action more than once in packages that can be installed
                # together has to be solved somewhere else, though.)
                for l in content.splitlines():
                        l = l.lstrip()
                        if not l or l[0] == "#":
                                continue

                        try:
                                action = actions.fromstr(l)
                        except actions.ActionError, e:
                                # Add the FMRI to the exception and re-raise
                                e.fmri = self.fmri
                                raise

                        if action.name == "set" and \
                            action.attrs["name"] == "authority":
                                # Translate old action to new.
                                action.attrs["name"] = "publisher"

                        if action.attrs.has_key("path"):
                                np = action.attrs["path"].lstrip(os.path.sep)
                                action.attrs["path"] = np

                        if not action.include_this(excludes):
                                continue

                        self.size += int(action.attrs.get("pkg.size", "0"))
                        self.actions.append(action)

                        if action.name not in self.actions_bytype:
                                self.actions_bytype[action.name] = [ action ]
                        else:
                                self.actions_bytype[action.name].append(action)
                        # add any set actions to attributes
                        if action.name == "set":
                                try:
                                        keyvalue = action.attrs["name"]
                                        if keyvalue not in self.attributes:
                                                self.attributes[keyvalue] = \
                                                    action.attrs["value"]
                                except KeyError: # ignore broken set actions
                                        pass

                        # append any variants and facets to manifest dict
                        v_list, f_list = action.get_varcet_keys()

                        if v_list or f_list:
                                for v, d in zip(v_list, repeat(self.variants)) \
                                    + zip(f_list, repeat(self.facets)):
                                        if v not in d:
                                                d[v] = set([action.attrs[v]])
                                        else:
                                                d[v].add(action.attrs[v])
                return

        @staticmethod
        def search_dict(file_path, excludes, return_line=False):
                file_handle = file(file_path)
                cur_pos = 0
                line = file_handle.readline()
                action_dict = {}
                def __handle_list(lst, cp):
                        for action_name, subtype, tok, full_value in lst:
                                if action_name == "set":
                                        if full_value is None:
                                                full_value = tok
                                else:
                                        if full_value is None:
                                                full_value = subtype
                                        if full_value is None:
                                                full_value = action_name
                                if isinstance(tok, list):
                                        __handle_list([
                                            (action_name, subtype, t,
                                            full_value)
                                            for t in tok
                                        ], cp)
                                else:
                                        if (tok, action_name, subtype,
                                            full_value) in action_dict:
                                                action_dict[(tok, action_name,
                                                    subtype, full_value)
                                                    ].append(cp)
                                        else:
                                                action_dict[(tok, action_name,
                                                    subtype, full_value)] = [cp]
                while line:
                        l = line.strip()
                        if l and l[0] != "#":
                                action = actions.fromstr(l)
                                if action.include_this(excludes):
                                        if action.attrs.has_key("path"):
                                                np = action.attrs["path"].lstrip(os.path.sep)
                                                action.attrs["path"] = np
                                        arg = cur_pos
                                        if return_line:
                                                arg = l
                                        __handle_list(action.generate_indices(),
                                            arg)
                        cur_pos = file_handle.tell()
                        line = file_handle.readline()
                file_handle.close()
                return action_dict

        def store(self, mfst_path):
                """Store the manifest contents to disk."""

                try:
                        mfile = file(mfst_path, "w")
                except IOError:
                        try:
                                os.makedirs(os.path.dirname(mfst_path))
                        except OSError, e:
                                if e.errno != errno.EEXIST:
                                        raise
                        mfile = file(mfst_path, "w")

                #
                # We specifically avoid sorting manifests before writing
                # them to disk-- there's really no point in doing so, since
                # we'll sort actions globally during packaging operations.
                #
                mfile.write(self.tostr_unsorted())
                mfile.close()

        def get_variants(self, name):
                if name not in self.attributes:
                        return None
                variants = self.attributes[name]
                if not isinstance(variants, str):
                        return variants
                return [variants]

        def get(self, key, default):
                try:
                        return self[key]
                except KeyError:
                        return default

        def __getitem__(self, key):
                """Return the value for the package attribute 'key'."""
                return self.attributes[key]

        def __setitem__(self, key, value):
                """Set the value for the package attribute 'key' to 'value'."""
                self.attributes[key] = value
                for a in self.actions:
                        if a.name == "set" and a.attrs["name"] == key:
                                a.attrs["value"] = value
                                return

                new_attr = AttributeAction(None, name=key, value=value)
                self.actions.append(new_attr)

        def __contains__(self, key):
                return key in self.attributes

null = Manifest()
