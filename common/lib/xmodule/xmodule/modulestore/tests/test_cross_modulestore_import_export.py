"""
This suite of tests verifies that courses exported from one modulestore can be imported into
another modulestore and the result will be identical (ignoring changes to identifiers that are
the result of being imported into a course with a different course id).

It does this by providing facilities for creating and cleaning up each of the modulestore types,
and then for each combination of modulestores, performing the sequence:
    1) use xml_importer to read a course from xml from disk into the first modulestore (called the source)
    2) use xml_exporter to dump the course from the source modulestore to disk
    3) use xml_importer to read the dumped course into a second modulestore (called the destination)
    4) Compare all modules in the source and destination modulestores to make sure that they line up

"""
from contextlib import contextmanager, nested
import itertools
from path import path
import random
from shutil import rmtree
from tempfile import mkdtemp

import ddt
from nose.plugins.attrib import attr

from xmodule.tests import CourseComparisonTest
from xmodule.modulestore.mongo.base import ModuleStoreEnum
from xmodule.modulestore.mongo.draft import DraftModuleStore
from xmodule.modulestore.mixed import MixedModuleStore
from xmodule.contentstore.mongo import MongoContentStore
from xmodule.modulestore.xml_importer import import_from_xml
from xmodule.modulestore.xml_exporter import export_to_xml
from xmodule.modulestore.split_mongo.split_draft import DraftVersioningModuleStore
from xmodule.modulestore.tests.mongo_connection import MONGO_PORT_NUM, MONGO_HOST
from xmodule.modulestore.inheritance import InheritanceMixin
from xmodule.partitions.tests.test_partitions import PartitionTestCase
from xmodule.x_module import XModuleMixin
from xmodule.modulestore.xml import XMLModuleStore

TEST_DATA_DIR = 'common/test/data/'


COMMON_DOCSTORE_CONFIG = {
    'host': MONGO_HOST,
    'port': MONGO_PORT_NUM,
}
DATA_DIR = path(__file__).dirname().parent.parent / "tests" / "data" / "xml-course-root"

XBLOCK_MIXINS = (InheritanceMixin, XModuleMixin)


class MemoryCache(object):
    """
    This fits the metadata_inheritance_cache_subsystem interface used by
    the modulestore, and stores the data in a dictionary in memory.
    """
    def __init__(self):
        self._data = {}

    def get(self, key, default=None):
        """
        Get a key from the cache.

        Args:
            key: The key to update.
            default: The value to return if the key hasn't been set previously.
        """
        return self._data.get(key, default)

    def set(self, key, value):
        """
        Set a key in the cache.

        Args:
            key: The key to update.
            value: The value change the key to.
        """
        self._data[key] = value


class MongoContentstoreBuilder(object):
    """
    A builder class for a MongoContentStore.
    """
    @contextmanager
    def build(self):
        """
        A contextmanager that returns a MongoContentStore, and deletes its contents
        when the context closes.
        """
        contentstore = MongoContentStore(
            db='contentstore{}'.format(random.randint(0, 10000)),
            collection='content',
            **COMMON_DOCSTORE_CONFIG
        )
        contentstore.ensure_indexes()

        try:
            yield contentstore
        finally:
            # Delete the created database
            contentstore._drop_database()

    def __repr__(self):
        return 'MongoContentstoreBuilder()'


class StoreBuilderBase(object):
    """
    Base class for all modulestore builders.
    """
    @contextmanager
    def build(self, **kwargs):
        contentstore = kwargs.pop('contentstore', None)
        if not contentstore:
            with self.build_without_contentstore() as (contentstore, modulestore):
                yield contentstore, modulestore
        else:
            with self.build_with_contentstore(contentstore) as modulestore:
                yield modulestore

    @contextmanager
    def build_without_contentstore(self):
        with MongoContentstoreBuilder().build() as contentstore:
            with self.build_with_contentstore(contentstore) as modulestore:
                yield contentstore, modulestore


class MongoModulestoreBuilder(StoreBuilderBase):
    """
    A builder class for a DraftModuleStore.
    """
    @contextmanager
    def build_with_contentstore(self, contentstore):
        """
        A contextmanager that returns an isolated mongo modulestore, and then deletes
        all of its data at the end of the context.

        Args:
            contentstore: The contentstore that this modulestore should use to store
                all of its assets.
        """
        doc_store_config = dict(
            db='modulestore{}'.format(random.randint(0, 10000)),
            collection='xmodule',
            asset_collection='asset_metadata',
            **COMMON_DOCSTORE_CONFIG
        )

        # Set up a temp directory for storing filesystem content created during import
        fs_root = mkdtemp()

        # pylint: disable=attribute-defined-outside-init
        modulestore = DraftModuleStore(
            contentstore,
            doc_store_config,
            fs_root,
            render_template=repr,
            branch_setting_func=lambda: ModuleStoreEnum.Branch.draft_preferred,
            metadata_inheritance_cache_subsystem=MemoryCache(),
            xblock_mixins=XBLOCK_MIXINS,
        )
        modulestore.ensure_indexes()

        try:
            yield modulestore
        finally:
            # Delete the created database
            modulestore._drop_database()  # pylint: disable=protected-access

            # Delete the created directory on the filesystem
            rmtree(fs_root, ignore_errors=True)

    def __repr__(self):
        return 'MongoModulestoreBuilder()'


class VersioningModulestoreBuilder(StoreBuilderBase):
    """
    A builder class for a VersioningModuleStore.
    """
    @contextmanager
    def build_with_contentstore(self, contentstore):
        """
        A contextmanager that returns an isolated versioning modulestore, and then deletes
        all of its data at the end of the context.

        Args:
            contentstore: The contentstore that this modulestore should use to store
                all of its assets.
        """
        # pylint: disable=unreachable
        doc_store_config = dict(
            db='modulestore{}'.format(random.randint(0, 10000)),
            collection='split_module',
            **COMMON_DOCSTORE_CONFIG
        )
        # Set up a temp directory for storing filesystem content created during import
        fs_root = mkdtemp()

        modulestore = DraftVersioningModuleStore(
            contentstore,
            doc_store_config,
            fs_root,
            render_template=repr,
            xblock_mixins=XBLOCK_MIXINS,
        )
        modulestore.ensure_indexes()

        try:
            yield modulestore
        finally:
            # Delete the created database
            modulestore._drop_database()  # pylint: disable=protected-access

            # Delete the created directory on the filesystem
            rmtree(fs_root, ignore_errors=True)

    def __repr__(self):
        return 'SplitModulestoreBuilder()'


class XmlModulestoreBuilder(StoreBuilderBase):
    """
    A builder class for a XMLModuleStore.
    """
    # pylint: disable=unused-argument
    @contextmanager
    def build_with_contentstore(self, contentstore=None, course_ids=None):
        """
        A contextmanager that returns an isolated xml modulestore

        Args:
            contentstore: The contentstore that this modulestore should use to store
                all of its assets.
        """
        modulestore = XMLModuleStore(
            DATA_DIR,
            course_ids=course_ids,
            default_class='xmodule.hidden_module.HiddenDescriptor',
            xblock_mixins=XBLOCK_MIXINS,
        )

        yield modulestore


class MixedModulestoreBuilder(StoreBuilderBase):
    """
    A builder class for a MixedModuleStore.
    """
    def __init__(self, store_builders, mappings=None):
        """
        Args:
            store_builders: A list of modulestore builder objects. These will be instantiated, in order,
                as the backing stores for the MixedModuleStore.
            mappings: Any course mappings to pass to the MixedModuleStore on instantiation.
        """
        self.store_builders = store_builders
        self.mappings = mappings or {}
        self.mixed_modulestore = None

    @contextmanager
    def build_with_contentstore(self, contentstore):
        """
        A contextmanager that returns a mixed modulestore built on top of modulestores
        generated by other builder classes.

        Args:
            contentstore: The contentstore that this modulestore should use to store
                all of its assets.
        """
        names, generators = zip(*self.store_builders)

        with nested(*(gen.build_with_contentstore(contentstore) for gen in generators)) as modulestores:
            # Make the modulestore creation function just return the already-created modulestores
            store_iterator = iter(modulestores)
            create_modulestore_instance = lambda *args, **kwargs: store_iterator.next()

            # Generate a fake list of stores to give the already generated stores appropriate names
            stores = [{'NAME': name, 'ENGINE': 'This space deliberately left blank'} for name in names]

            self.mixed_modulestore = MixedModuleStore(
                contentstore,
                self.mappings,
                stores,
                create_modulestore_instance=create_modulestore_instance,
                xblock_mixins=XBLOCK_MIXINS,
            )

            yield self.mixed_modulestore

    def __repr__(self):
        return 'MixedModulestoreBuilder({!r}, {!r})'.format(self.store_builders, self.mappings)

    def asset_collection(self):
        """
        Returns the collection storing the asset metadata.
        """
        all_stores = self.mixed_modulestore.modulestores
        if len(all_stores) > 1:
            return None

        store = all_stores[0]
        if hasattr(store, 'asset_collection'):
            # Mongo modulestore beneath mixed.
            # Returns the entire collection with *all* courses' asset metadata.
            return store.asset_collection
        else:
            # Split modulestore beneath mixed.
            # Split stores all asset metadata in the structure collection.
            return store.db_connection.structures


MIXED_MODULESTORE_BOTH_SETUP = MixedModulestoreBuilder([
    ('draft', MongoModulestoreBuilder()),
    ('split', VersioningModulestoreBuilder())
])
MIXED_MODULESTORE_SETUPS = (
    MixedModulestoreBuilder([('draft', MongoModulestoreBuilder())]),
    MixedModulestoreBuilder([('split', VersioningModulestoreBuilder())]),
)
MIXED_MS_SETUPS_SHORT = (
    'mixed_mongo',
    'mixed_split',
)
DIRECT_MODULESTORE_SETUPS = (
    MongoModulestoreBuilder(),
    # VersioningModulestoreBuilder(),  # FUTUREDO: LMS-11227
)
DIRECT_MS_SETUPS_SHORT = (
    'mongo',
    #'split',
)
MODULESTORE_SETUPS = DIRECT_MODULESTORE_SETUPS + MIXED_MODULESTORE_SETUPS
MODULESTORE_SHORTNAMES = DIRECT_MS_SETUPS_SHORT + MIXED_MS_SETUPS_SHORT
SHORT_NAME_MAP = dict(zip(MODULESTORE_SETUPS, MODULESTORE_SHORTNAMES))

CONTENTSTORE_SETUPS = (MongoContentstoreBuilder(),)
COURSE_DATA_NAMES = (
    'toy',
    'manual-testing-complete',
    'split_test_module',
    'split_test_module_draft',
)


@ddt.ddt
@attr('mongo')
class CrossStoreXMLRoundtrip(CourseComparisonTest, PartitionTestCase):
    """
    This class exists to test XML import and export between different modulestore
    classes.
    """

    def setUp(self):
        super(CrossStoreXMLRoundtrip, self).setUp()
        self.export_dir = mkdtemp()
        self.addCleanup(rmtree, self.export_dir, ignore_errors=True)

    @ddt.data(*itertools.product(
        MODULESTORE_SETUPS,
        MODULESTORE_SETUPS,
        CONTENTSTORE_SETUPS,
        CONTENTSTORE_SETUPS,
        COURSE_DATA_NAMES,
    ))
    @ddt.unpack
    def test_round_trip(self, source_builder, dest_builder, source_content_builder, dest_content_builder, course_data_name):
        # Construct the contentstore for storing the first import
        with source_content_builder.build() as source_content:
            # Construct the modulestore for storing the first import (using the previously created contentstore)
            with source_builder.build(contentstore=source_content) as source_store:
                # Construct the contentstore for storing the second import
                with dest_content_builder.build() as dest_content:
                    # Construct the modulestore for storing the second import (using the second contentstore)
                    with dest_builder.build(contentstore=dest_content) as dest_store:
                        source_course_key = source_store.make_course_key('a', 'course', 'course')
                        dest_course_key = dest_store.make_course_key('a', 'course', 'course')

                        import_from_xml(
                            source_store,
                            'test_user',
                            TEST_DATA_DIR,
                            course_dirs=[course_data_name],
                            static_content_store=source_content,
                            target_course_id=source_course_key,
                            create_course_if_not_present=True,
                            raise_on_failure=True,
                        )

                        export_to_xml(
                            source_store,
                            source_content,
                            source_course_key,
                            self.export_dir,
                            'exported_source_course',
                        )

                        import_from_xml(
                            dest_store,
                            'test_user',
                            self.export_dir,
                            course_dirs=['exported_source_course'],
                            static_content_store=dest_content,
                            target_course_id=dest_course_key,
                            create_course_if_not_present=True,
                            raise_on_failure=True,
                        )

                        # NOT CURRENTLY USED
                        # export_to_xml(
                        #     dest_store,
                        #     dest_content,
                        #     dest_course_key,
                        #     self.export_dir,
                        #     'exported_dest_course',
                        # )

                        self.exclude_field(None, 'wiki_slug')
                        self.exclude_field(None, 'xml_attributes')
                        self.exclude_field(None, 'parent')
                        self.ignore_asset_key('_id')
                        self.ignore_asset_key('uploadDate')
                        self.ignore_asset_key('content_son')
                        self.ignore_asset_key('thumbnail_location')

                        self.assertCoursesEqual(
                            source_store,
                            source_course_key,
                            dest_store,
                            dest_course_key,
                        )

                        self.assertAssetsEqual(
                            source_content,
                            source_course_key,
                            dest_content,
                            dest_course_key,
                        )

                        self.assertAssetsMetadataEqual(
                            source_store,
                            source_course_key,
                            dest_store,
                            dest_course_key,
                        )
