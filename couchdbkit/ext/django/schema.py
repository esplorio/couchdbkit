# -*- coding: utf-8 -*-
#
# Copyright (c) 2008-2009 Benoit Chesneau <benoitc@e-engura.com>
#
# Permission to use, copy, modify, and distribute this software for any
# purpose with or without fee is hereby granted, provided that the above
# copyright notice and this permission notice appear in all copies.
#
# THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
# WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
# ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
# WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
# ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
# OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

""" Wrapper of couchdbkit Document and Properties for django. It also
add possibility to a document to register itself in CouchdbkitHandler
"""
import re
import sys
from collections import OrderedDict

try:
    from django.db.models.options import get_verbose_name
except ImportError:
    from django.utils.text import camel_case_to_spaces as get_verbose_name

from django.apps import apps
from django.conf import settings
from django.core.exceptions import FieldDoesNotExist
from django.utils.translation import (activate, deactivate_all,
                                      get_language, string_concat)
from django.utils.encoding import smart_str, force_unicode
from django.utils.functional import cached_property

from django.db.models.fields import Field

from couchdbkit import schema
from couchdbkit.ext.django.loading import get_schema, register_schema, get_db

__all__ = ['Property', 'StringProperty', 'IntegerProperty',
            'DecimalProperty', 'BooleanProperty', 'FloatProperty',
            'DateTimeProperty', 'DateProperty', 'TimeProperty',
            'dict_to_json', 'list_to_json', 'value_to_json',
            'value_to_python', 'dict_to_python', 'list_to_python',
            'convert_property', 'DocumentSchema', 'Document',
            'SchemaProperty', 'SchemaListProperty', 'ListProperty',
            'DictProperty', 'StringDictProperty', 'StringListProperty',
            'SchemaDictProperty', 'SetProperty',]


DEFAULT_NAMES = ('verbose_name', 'db_table', 'ordering',
                 'app_label')

PROXY_PARENTS = object()


class Options(object):
    """ class based on django.db.models.options. We only keep
    useful bits."""

    def __init__(self, meta, app_label=None):
        self.module_name, self.verbose_name = None, None
        self.verbose_name_plural = None
        self.object_name, self.app_label = None, app_label
        self.meta = meta
        self.admin = None
        self.abstract = False
        self._get_fields_cache = {}
        self.local_fields = []
        self.local_many_to_many = []
        self.virtual_fields = []
        self.parents = OrderedDict()
        self.apps = apps
        self.concrete_model = None
        self.managed = True
        self.db_table = ''
        self.db_tablespace = settings.DEFAULT_TABLESPACE

    def contribute_to_class(self, cls, name):
        cls._meta = self
        cls._meta.pk = PKField()
        self.installed = re.sub('\.models$', '', cls.__module__) in settings.INSTALLED_APPS
        # First, construct the default values for these options.
        self.object_name = cls.__name__
        self.module_name = self.object_name.lower()
        self.verbose_name = get_verbose_name(self.object_name)
        self.model = cls
        self.concrete_model = cls

        # Next, apply any overridden values from 'class Meta'.
        if self.meta:
            meta_attrs = self.meta.__dict__.copy()
            for name in self.meta.__dict__:
                # Ignore any private attributes that Django doesn't care about.
                # NOTE: We can't modify a dictionary's contents while looping
                # over it, so we loop over the *original* dictionary instead.
                if name.startswith('_'):
                    del meta_attrs[name]
            for attr_name in DEFAULT_NAMES:
                if attr_name in meta_attrs:
                    setattr(self, attr_name, meta_attrs.pop(attr_name))
                elif hasattr(self.meta, attr_name):
                    setattr(self, attr_name, getattr(self.meta, attr_name))

            # verbose_name_plural is a special case because it uses a 's'
            # by default.
            setattr(self, 'verbose_name_plural', meta_attrs.pop('verbose_name_plural', string_concat(self.verbose_name, 's')))

            # Any leftover attributes must be invalid.
            if meta_attrs != {}:
                raise TypeError("'class Meta' got invalid attribute(s): %s" % ','.join(meta_attrs.keys()))
        else:
            self.verbose_name_plural = string_concat(self.verbose_name, 's')
        del self.meta

    def __str__(self):
        return "%s.%s" % (smart_str(self.app_label), smart_str(self.module_name))

    def verbose_name_raw(self):
        """
        There are a few places where the untranslated verbose name is needed
        (so that we get the same value regardless of currently active
        locale).
        """
        lang = get_language()
        deactivate_all()
        raw = force_unicode(self.verbose_name)
        activate(lang)
        return raw
    verbose_name_raw = property(verbose_name_raw)

    def get_field(self, field_name, many_to_many=None):
        """
        Returns a field instance given a field name. The field can be either a
        forward or reverse field, unless many_to_many is specified; if it is,
        only forward fields will be returned.
        The many_to_many argument exists for backwards compatibility reasons;
        it has been deprecated and will be removed in Django 2.0.
        """
        m2m_in_kwargs = many_to_many is not None
        try:
            # In order to avoid premature loading of the relation tree
            # (expensive) we prefer checking if the field is a forward field.
            field = self._forward_fields_map[field_name]

            if many_to_many is False and field.many_to_many:
                raise FieldDoesNotExist(
                    '%s has no field named %r' % (self.object_name, field_name)
                )

            return field
        except KeyError:
            # If the app registry is not ready, reverse fields are
            # unavailable, therefore we throw a FieldDoesNotExist exception.
            if not self.apps.models_ready:
                raise FieldDoesNotExist(
                    "%s has no field named %r. The app cache isn't ready yet, "
                    "so if this is an auto-created related field, it won't "
                    "be available yet." % (self.object_name, field_name)
                )

        try:
            if m2m_in_kwargs:
                # Previous API does not allow searching reverse fields.
                raise FieldDoesNotExist('%s has no field named %r' % (self.object_name, field_name))

            # Retrieve field instance by name from cached or just-computed
            # field map.
            return self.fields_map[field_name]
        except KeyError:
            raise FieldDoesNotExist('%s has no field named %r' % (self.object_name, field_name))

    def _get_fields(self, **_):
        """
        Provide a mostly no-op Django compatible API for Options._get_fields

        Since this is not being properly initialised by a django.db.models.Model
        subclass we have to work around various machinery that isn't being called,
        namely the populating of self.local_fields and various relation parts.

        Instead of copy and pasting the entire method we're simply relying on
        self.local_fields being populated by the model's metaclass.

        Note: Currently only User needs this for the USERNAME_FIELD to work.
        """
        return self.local_fields

    @cached_property
    def _forward_fields_map(self):
        res = {}
        fields = self._get_fields(reverse=False)
        for field in fields:
            res[field.name] = field
            # Due to the way Django's internals work, get_field() should also
            # be able to fetch a field by attname. In the case of a concrete
            # field with relation, includes the *_id name too
            try:
                res[field.attname] = field
            except AttributeError:
                pass
        return res

    @cached_property
    def fields_map(self):
        res = {}
        fields = self._get_fields(forward=False, include_hidden=True)
        for field in fields:
            res[field.name] = field
            # Due to the way Django's internals work, get_field() should also
            # be able to fetch a field by attname. In the case of a concrete
            # field with relation, includes the *_id name too
            try:
                res[field.attname] = field
            except AttributeError:
                pass
        return res


class PKField(Field):
    name = 'pk'
    attname = 'pk'

    def to_python(self, value, *args, **kwargs):
        return str(value)

    def value_to_string(self, obj):
        return str(obj.pk)


class DocumentMeta(schema.SchemaProperties):
    def __new__(cls, name, bases, attrs):
        super_new = super(DocumentMeta, cls).__new__
        parents = [b for b in bases if isinstance(b, DocumentMeta)]
        if not parents:
            return super_new(cls, name, bases, attrs)

        new_class = super_new(cls, name, bases, attrs)
        attr_meta = attrs.pop('Meta', None)
        if not attr_meta:
            meta = getattr(new_class, 'Meta', None)
        else:
            meta = attr_meta

        if getattr(meta, 'app_label', None) is None:
            document_module = sys.modules[new_class.__module__]
            app_label = document_module.__name__.split('.')[-2]
        else:
            app_label = getattr(meta, 'app_label')

        new_class.add_to_class('_meta', Options(meta, app_label=app_label))

        register_schema(app_label, new_class)

        return get_schema(app_label, name)

    def add_to_class(cls, name, value):
        if hasattr(value, 'contribute_to_class'):
            value.contribute_to_class(cls, name)
        else:
            setattr(cls, name, value)


class Document(schema.Document):
    """ Document object for django extension """
    __metaclass__ = DocumentMeta

    get_id = property(lambda self: self['_id'])
    get_rev = property(lambda self: self['_rev'])

    @classmethod
    def get_db(cls):
        db = getattr(cls, '_db', None)
        if db is None:
            app_label = getattr(cls._meta, "app_label")
            db = get_db(app_label)
            cls._db = db
        return db


DocumentSchema = schema.DocumentSchema

#  properties
Property = schema.Property
StringProperty = schema.StringProperty
IntegerProperty = schema.IntegerProperty
DecimalProperty = schema.DecimalProperty
BooleanProperty = schema.BooleanProperty
FloatProperty = schema.FloatProperty
DateTimeProperty = schema.DateTimeProperty
DateProperty = schema.DateProperty
TimeProperty = schema.TimeProperty
SchemaProperty = schema.SchemaProperty
SchemaListProperty = schema.SchemaListProperty
ListProperty = schema.ListProperty
DictProperty = schema.DictProperty
StringDictProperty = schema.StringDictProperty
StringListProperty = schema.StringListProperty
SchemaDictProperty = schema.SchemaDictProperty
SetProperty = schema.SetProperty



# some utilities
dict_to_json = schema.dict_to_json
list_to_json = schema.list_to_json
value_to_json = schema.value_to_json
value_to_python = schema.value_to_python
dict_to_python = schema.dict_to_python
list_to_python = schema.list_to_python
convert_property = schema.convert_property
