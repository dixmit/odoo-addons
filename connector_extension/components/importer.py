# Copyright NuoBiT Solutions - Eric Antones <eantones@nuobit.com>
# Copyright NuoBiT Solutions - Kilian Niubo <kniubo@nuobit.com>
# License LGPL-3.0 or later (http://www.gnu.org/licenses/lgpl.html)

import logging
from contextlib import contextmanager

import psycopg2

from odoo import _, fields
from odoo.exceptions import ValidationError

from odoo.addons.component.core import AbstractComponent
from odoo.addons.connector.exception import IDMissingInBackend
from odoo.addons.queue_job.exception import NothingToDoJob, RetryableJobError

_logger = logging.getLogger(__name__)


class ConnectorExtensionGenericDirectImporter(AbstractComponent):
    """Generic Synchronizer for importing data from backend to Odoo"""

    _name = "connector.extension.generic.record.direct.importer"
    _inherit = "base.importer"

    _usage = "record.direct.importer"

    @contextmanager
    def _retry_unique_violation(self):
        """Context manager: catch Unique constraint error and retry the
        job later.

        When we execute several jobs workers concurrently, it happens
        that 2 jobs are creating the same record at the same time (binding
        record created by :meth:`_export_dependency`), resulting in:

            IntegrityError: duplicate key value violates unique
            constraint "prestashop_product_template_openerp_uniq"
            DETAIL:  Key (backend_id, odoo_id)=(1, 4851) already exists.

        In that case, we'll retry the import just later.

        """
        try:
            yield
        except psycopg2.IntegrityError as e:
            if e.pgcode == psycopg2.errorcodes.UNIQUE_VIOLATION:
                raise RetryableJobError(
                    "A database error caused the failure of the job:\n"
                    "%s\n\n"
                    "Likely due to 2 concurrent jobs wanting to create "
                    "the same record. The job will be retried later." % e
                ) from e
            else:
                raise

    def _import_dependency(
        self,
        external_id,
        binding_model,
        sync_date,
        external_data=None,
        importer=None,
        adapter=None,
        always=False,
    ):
        """Import a dependency.

        The importer class is a class or subclass of
        :class:`<Backend>Importer`. A specific class can be defined.

        :param external_ids: id or id's of the related bindings to import
        :param binding_model: name of the binding model for the relation
        :type binding_model: str | unicode
        :param importer_component: component to use for import
                                  By default: 'importer'
        :type importer_component: Component
        :param adapter_component: component to use for access to backend
                                  By default: 'backend.adapter'
        :type adapter_component: Component
        :param always: if True, the record is updated even if it already
                      exists, note that it is still skipped if it has
                      not been modified on Backend since the last
                      update. When False, it will import it only when
                      it does not yet exist.
        :type always: boolean
        """
        if not external_id:
            return

        if importer is None:
            importer = self.component(usage=self._usage, model_name=binding_model)

        binder = self.binder_for(binding_model)

        if always or not binder.to_internal(external_id):
            try:
                importer.run(external_id, sync_date, external_data=external_data)
            except NothingToDoJob:
                _logger.info(
                    "Dependency import of %s(%s) has been ignored.",
                    binding_model._name,
                    external_id,
                )

    def _import_dependencies(self, external_data, sync_date, external_fields=None):
        """Import the dependencies for the record

        Import of dependencies can be done manually or by calling
        :meth:`_import_dependency` for each dependency.
        """
        return

    def _after_import(self, binding):
        return

    def _must_skip(self, binding):
        """Hook called right after we read the data from the backend.

        If the method returns a message giving a reason for the
        skipping, the import will be interrupted and the message
        recorded in the job (if the import is called directly by the
        job, not by dependencies).

        If it returns None, the import will continue normally.

        :returns: None | str | unicode
        """
        return False

    def _mapper_options(self, binding, sync_date):
        return {"binding": binding, "sync_date": sync_date}

    def run(self, external_id, sync_date, external_data=None, external_fields=None):
        if not external_data:
            external_data = {}
        lock_name = "import({}, {}, {}, {})".format(
            self.backend_record._name,
            self.backend_record.id,
            self.work.model_name,
            external_id,
        )
        # Keep a lock on this import until the transaction is committed
        # The lock is kept since we have detected that the informations
        # will be updated into Odoo
        self.advisory_lock_or_retry(lock_name, retry_seconds=10)
        if not external_data:
            # read external data from Backend
            external_data = self.backend_adapter.read(external_id)
            if not external_data:
                raise IDMissingInBackend(
                    _("Record with external_id '%s' does not exist in Backend")
                    % (external_id,)
                )

        # import the missing linked resources
        self._import_dependencies(external_data, sync_date)

        # map_data
        # this one knows how to convert backend data to odoo data
        mapper = self.component(usage="import.mapper")

        # convert to odoo data
        internal_data = mapper.map_record(external_data)

        # get_binding
        # this one knows how to link Baclend/Odoo records
        binder = self.component(usage="binder")

        # find if the external id already exists in odoo
        binding = binder.to_internal(external_id)

        # if binding not exists, try to link existing internal object
        if not binding:
            binding = binder.to_binding_from_external_key(internal_data, sync_date)

        # skip binding
        skip = self._must_skip(binding)
        if skip:
            return skip

        # passing info to the mapper
        opts = self._mapper_options(binding, sync_date)

        if external_fields != [] or external_fields is None:
            # persist data
            if binding:
                # if exists, we update it
                values = internal_data.values(fields=external_fields, **opts)
                binder.bind_import(external_data, values, sync_date)
                self._update(binding, values)
                _logger.debug("%d updated from Backend %s", binding, external_id)
            else:
                # or we create it
                values = internal_data.values(
                    for_create=True, fields=external_fields, **opts
                )
                binder.bind_import(external_data, values, sync_date, for_create=True)
                binding = self._create(values)
                _logger.debug("%d created from Backend %s", binding, external_id)

            # last update
            self._after_import(binding)
        return True

    def _validate_update_data(self, data):
        """Check if the values to import are correct

        Pro-actively check before the ``Model.update`` if some fields
        are missing or invalid

        Raise `InvalidDataError`
        """
        return

    def _update(self, binding, data):
        """Update the Internal record"""
        self._validate_update_data(data)
        return binding.with_context(connector_no_export=True).write(data)

    def _validate_create_data(self, data):
        """Check if the values to import are correct

        Pro-actively check before the ``Model.create`` if some fields
        are missing or invalid

        Raise `InvalidDataError`
        """
        return

    def _create(self, data):
        """Create the Internal record"""
        self._validate_create_data(data)
        return self.model.with_context(connector_no_export=True).create(data)


class ConnectorExtensionGenericBatchImporter(AbstractComponent):
    """Generic Synchronizer for importing data from backend to Odoo"""

    _name = "connector.extension.generic.batch.importer"
    _inherit = "base.importer"

    _usage = "batch.importer"

    def run(self, domain=None):
        """Run the synchronization"""
        if domain is None:
            domain = []
        chunk_size = self.backend_record.chunk_size
        if chunk_size > 0:
            total_items = self.backend_adapter.get_total_items(domain=domain)
            if total_items == 0:
                return
            offset = 0
            while total_items > 0:
                if chunk_size > total_items:
                    chunk_size = total_items
                self._import_chunk(domain, offset, chunk_size)
                offset += chunk_size
                total_items -= chunk_size
        else:
            sync_date = fields.Datetime.now()
            data, len_items = self.backend_adapter.search_read(domain)
            for d in data:
                external_id = self.binder_for().dict2id(d, in_field=False)
                self._import_record(external_id, sync_date, external_data=d)

    def _import_chunk(self, domain, offset, chunk_size):
        raise NotImplementedError

    def _import_batch(self, domain):
        raise NotImplementedError

    def _import_record(self, external_id, sync_date, external_data=None):
        """Import a record directly or delay the import of the record.

        Method to implement in sub-classes.
        """
        raise NotImplementedError


class ConnectorExtensionBatchDirectImporter(AbstractComponent):
    """Import the records directly, without delaying the jobs."""

    _name = "connector.extension.generic.batch.direct.importer"
    _inherit = "connector.extension.generic.batch.importer"

    _usage = "batch.direct.importer"

    def _import_chunk(self, domain, offset, chunk_size):
        self.model.import_chunk(self.backend_record, domain, offset, chunk_size)

    def _import_batch(self, domain):
        self.model.import_batch(self.backend_record, domain)

    def _import_record(self, external_id, sync_date, external_data=None):
        """Import the record directly"""
        if external_data is None:
            external_data = {}
        self.model.import_record(
            self.backend_record, external_id, sync_date, external_data=external_data
        )


class ConnectorExtensionBatchDelayedImporter(AbstractComponent):
    """Delay import of the records"""

    _name = "connector.extension.generic.batch.delayed.importer"
    _inherit = "connector.extension.generic.batch.importer"

    _usage = "batch.delayed.importer"

    def _import_chunk(self, domain, offset, chunk_size):
        delayable = self.model.with_delay()
        delayable.import_chunk(self.backend_record, domain, offset, chunk_size)

    def _import_batch(self, domain):
        delayable = self.model.with_delay()
        delayable.import_batch(self.backend_record, domain)

    def _import_record(
        self, external_id, sync_date, external_data=None, job_options=None
    ):
        """Delay the import of the records"""
        if external_data is None:
            external_data = {}
        delayable = self.model.with_delay(**job_options or {})
        delayable.import_record(
            self.backend_record, external_id, sync_date, external_data=external_data
        )


class ConnectorExtensionGenericChunkImporter(AbstractComponent):
    """The role of a ChunkImporter is to search for a list of
    items to import, then it can either import them directly or delay
    the import of each item separately.
    """

    _name = "connector.extension.generic.chunk.importer"
    _inherit = "base.importer"

    def run(self, domain, offset, chunk_size):
        """Run the synchronization"""
        sync_date = fields.Datetime.now()
        data, len_items = self.backend_adapter.search_read(domain, offset, chunk_size)
        chunk_size -= len_items
        offset += len_items
        if chunk_size < 0:
            raise ValidationError(_("Unexpected Error: Chunk_size is < 0"))
        if chunk_size != 0:
            self.get_batch_importer()._import_chunk(domain, offset, chunk_size)
        for d in data:
            external_id = self.binder_for().dict2id(d, in_field=False)
            self._import_record(external_id, sync_date, external_data=d)

    def get_batch_importer(self):
        raise NotImplementedError

    def _import_record(self, external_id, sync_date, external_data=None):
        """Import a record directly or delay the import of the record.

        Method to implement in sub-classes.
        """
        raise NotImplementedError


class ConnectorExtensionChunkDirectImporter(AbstractComponent):
    """Import the records directly, without delaying the jobs."""

    _name = "connector.extension.generic.chunk.direct.importer"
    _inherit = "connector.extension.generic.chunk.importer"

    _usage = "chunk.direct.importer"

    def get_batch_importer(self):
        return self.component(usage="batch.direct.importer")

    def _import_record(self, external_id, sync_date, external_data=None):
        """Import the record directly"""
        if external_data is None:
            external_data = {}
        self.model.import_record(
            self.backend_record, external_id, sync_date, external_data=external_data
        )


class ConnectorExtensionChunkDelayedImporter(AbstractComponent):
    """Delay import of the records"""

    _name = "connector.extension.generic.chunk.delayed.importer"
    _inherit = "connector.extension.generic.chunk.importer"

    _usage = "chunk.delayed.importer"

    def get_batch_importer(self):
        return self.component(usage="batch.delayed.importer")

    def _import_record(
        self, external_id, sync_date, external_data=None, job_options=None
    ):
        """Delay the import of the records"""
        if external_data is None:
            external_data = {}
        delayable = self.model.with_delay(**job_options or {})
        delayable.import_record(
            self.backend_record, external_id, sync_date, external_data=external_data
        )
