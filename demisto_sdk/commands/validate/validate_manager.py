import os
import re
import sys
from configparser import ConfigParser, MissingSectionHeaderError
from typing import Optional

import click
from demisto_sdk.commands.common import tools
from demisto_sdk.commands.common.configuration import Configuration
from demisto_sdk.commands.common.constants import (API_MODULES_PACK,
                                                   CONTENT_ENTITIES_DIRS,
                                                   IGNORED_PACK_NAMES,
                                                   OLDEST_SUPPORTED_VERSION,
                                                   PACKS_DIR,
                                                   PACKS_PACK_META_FILE_NAME,
                                                   TESTS_AND_DOC_DIRECTORIES,
                                                   FileType)
from demisto_sdk.commands.common.content import Content
from demisto_sdk.commands.common.errors import (ALLOWED_IGNORE_ERRORS,
                                                ERROR_CODE,
                                                FOUND_FILES_AND_ERRORS,
                                                FOUND_FILES_AND_IGNORED_ERRORS,
                                                PRESET_ERROR_TO_CHECK,
                                                PRESET_ERROR_TO_IGNORE, Errors)
from demisto_sdk.commands.common.git_util import GitUtil
from demisto_sdk.commands.common.hook_validations.base_validator import \
    BaseValidator
from demisto_sdk.commands.common.hook_validations.classifier import \
    ClassifierValidator
from demisto_sdk.commands.common.hook_validations.conf_json import \
    ConfJsonValidator
from demisto_sdk.commands.common.hook_validations.dashboard import \
    DashboardValidator
from demisto_sdk.commands.common.hook_validations.id import IDSetValidations
from demisto_sdk.commands.common.hook_validations.image import ImageValidator
from demisto_sdk.commands.common.hook_validations.incident_field import \
    IncidentFieldValidator
from demisto_sdk.commands.common.hook_validations.incident_type import \
    IncidentTypeValidator
from demisto_sdk.commands.common.hook_validations.integration import \
    IntegrationValidator
from demisto_sdk.commands.common.hook_validations.layout import (
    LayoutsContainerValidator, LayoutValidator)
from demisto_sdk.commands.common.hook_validations.mapper import MapperValidator
from demisto_sdk.commands.common.hook_validations.pack_unique_files import \
    PackUniqueFilesValidator
from demisto_sdk.commands.common.hook_validations.playbook import \
    PlaybookValidator
from demisto_sdk.commands.common.hook_validations.readme import ReadMeValidator
from demisto_sdk.commands.common.hook_validations.release_notes import \
    ReleaseNotesValidator
from demisto_sdk.commands.common.hook_validations.report import ReportValidator
from demisto_sdk.commands.common.hook_validations.reputation import \
    ReputationValidator
from demisto_sdk.commands.common.hook_validations.script import ScriptValidator
from demisto_sdk.commands.common.hook_validations.structure import \
    StructureValidator
from demisto_sdk.commands.common.hook_validations.test_playbook import \
    TestPlaybookValidator
from demisto_sdk.commands.common.hook_validations.widget import WidgetValidator
from demisto_sdk.commands.common.tools import (find_type, get_api_module_ids,
                                               get_api_module_integrations_set,
                                               get_content_release_identifier,
                                               get_pack_ignore_file_path,
                                               get_pack_name,
                                               get_pack_names_from_files,
                                               get_yaml, open_id_set_file,
                                               run_command)
from demisto_sdk.commands.create_id_set.create_id_set import IDSetCreator
from git import InvalidGitRepositoryError
from packaging import version


class ValidateManager:
    def __init__(
            self, is_backward_check=True, prev_ver=None, use_git=False, only_committed_files=False,
            print_ignored_files=False, skip_conf_json=True, validate_id_set=False, file_path=None,
            validate_all=False, is_external_repo=False, skip_pack_rn_validation=False, print_ignored_errors=False,
            silence_init_prints=False, no_docker_checks=False, skip_dependencies=False, id_set_path=None, staged=False,
            create_id_set=False
    ):
        # General configuration
        self.skip_docker_checks = False
        self.no_configuration_prints = silence_init_prints
        self.skip_conf_json = skip_conf_json
        self.is_backward_check = is_backward_check
        self.is_circle = only_committed_files
        self.validate_all = validate_all
        self.use_git = use_git
        self.skip_pack_rn_validation = skip_pack_rn_validation
        self.prev_ver = prev_ver if prev_ver else 'origin/master'
        self.print_ignored_files = print_ignored_files
        self.print_ignored_errors = print_ignored_errors
        self.skip_dependencies = skip_dependencies or not use_git
        self.skip_id_set_creation = not create_id_set or self.skip_dependencies
        self.compare_type = '...'
        self.staged = staged

        # Class constants
        self.handle_error = BaseValidator(print_as_warnings=print_ignored_errors).handle_error
        self.file_path = file_path
        self.id_set_path = id_set_path or "./Tests/id_set.json"
        # create the id_set only once per run.
        self.id_set_file = self.get_id_set_file(self.skip_id_set_creation, self.id_set_path)

        self.id_set_validations = IDSetValidations(is_circle=self.is_circle,
                                                   configuration=Configuration(),
                                                   ignored_errors=None,
                                                   print_as_warnings=self.print_ignored_errors,
                                                   id_set_file=self.id_set_file) \
            if validate_id_set else None

        try:
            self.git_util = GitUtil(repo=Content.git())
            self.branch_name = self.git_util.get_current_working_branch()
        except InvalidGitRepositoryError:
            if self.use_git:
                raise
            else:
                self.git_util = None  # type: ignore
                self.branch_name = ''
                pass

        self.branch_name = ''
        self.changes_in_schema = False
        self.check_only_schema = False
        self.always_valid = False
        self.ignored_files = set()
        self.new_packs = set()
        self.skipped_file_types = (FileType.CHANGELOG,
                                   FileType.DESCRIPTION,
                                   FileType.DOC_IMAGE,
                                   FileType.PACK_METADATA)

        self.is_external_repo = is_external_repo
        if is_external_repo:
            if not self.no_configuration_prints:
                click.echo('Running in a private repository')
            self.skip_conf_json = True

        self.print_percent = False
        self.completion_percentage = 0

        if validate_all:
            # No need to check docker images on build branch hence we do not check on -a mode
            # also do not skip id set creation unless the flag is up
            self.skip_docker_checks = True
            self.skip_pack_rn_validation = True
            self.print_percent = True

        if no_docker_checks:
            self.skip_docker_checks = True

    def print_final_report(self, valid):
        self.print_ignored_files_report(self.print_ignored_files)
        self.print_ignored_errors_report(self.print_ignored_errors)

        if valid:
            click.secho('\nThe files are valid', fg='green')
            return 0

        else:
            all_failing_files = '\n'.join(FOUND_FILES_AND_ERRORS)
            click.secho(f"\n=========== Found errors in the following files ===========\n\n{all_failing_files}\n",
                        fg="bright_red")

            if self.always_valid:
                click.secho('Found the errors above, but not failing build', fg='yellow')
                return 0

            click.secho('The files were found as invalid, the exact error message can be located above',
                        fg='red')
            return 1

    def run_validation(self):
        """Initiates validation in accordance with mode (i,g,a)
        """
        if self.validate_all:
            is_valid = self.run_validation_on_all_packs()
        elif self.use_git:
            is_valid = self.run_validation_using_git()
        elif self.file_path:
            is_valid = self.run_validation_on_specific_files()
        else:
            # default validate to -g --post-commit
            self.use_git = True
            self.is_circle = True
            is_valid = self.run_validation_using_git()
        return self.print_final_report(is_valid)

    def run_validation_on_specific_files(self):
        """Run validations only on specific files
        """
        files_validation_result = set()

        for path in self.file_path.split(','):
            error_ignore_list = self.get_error_ignore_list(get_pack_name(path))

            if os.path.isfile(path):
                click.secho('\n================= Validating file =================', fg="bright_cyan")
                files_validation_result.add(self.run_validations_on_file(path, error_ignore_list))

            else:
                path = path.rstrip('/')
                dir_name = os.path.basename(path)
                if dir_name in CONTENT_ENTITIES_DIRS:
                    click.secho(f'\n================= Validating content directory {path} =================',
                                fg="bright_cyan")
                    files_validation_result.add(self.run_validation_on_content_entities(path, error_ignore_list))
                else:
                    if os.path.basename(os.path.dirname(path)) == PACKS_DIR:
                        click.secho(f'\n================= Validating pack {path} =================',
                                    fg="bright_cyan")
                        files_validation_result.add(self.run_validations_on_pack(path))

                    else:
                        click.secho(f'\n================= Validating package {path} =================',
                                    fg="bright_cyan")
                        files_validation_result.add(self.run_validation_on_package(path, error_ignore_list))

        return all(files_validation_result)

    def run_validation_on_all_packs(self):
        """Runs validations on all files in all packs in repo (-a option)

        Returns:
            bool. true if all files are valid, false otherwise.
        """
        click.secho('\n================= Validating all files =================', fg="bright_cyan")
        all_packs_valid = set()

        if not self.skip_conf_json:
            conf_json_validator = ConfJsonValidator()
            all_packs_valid.add(conf_json_validator.is_valid_conf_json())

        num_of_packs = len(os.listdir(PACKS_DIR))
        count = 1

        for pack_name in os.listdir(PACKS_DIR):
            self.completion_percentage = format((count / num_of_packs) * 100, ".2f")  # type: ignore
            pack_path = os.path.join(PACKS_DIR, pack_name)
            all_packs_valid.add(self.run_validations_on_pack(pack_path))
            count += 1

        return all(all_packs_valid)

    def run_validations_on_pack(self, pack_path):
        """Runs validation on all files in given pack. (i,g,a)

        Args:
            pack_path: the path to the pack.

        Returns:
            bool. true if all files in pack are valid, false otherwise.
        """
        pack_entities_validation_results = set()
        pack_error_ignore_list = self.get_error_ignore_list(os.path.basename(pack_path))

        pack_entities_validation_results.add(self.validate_pack_unique_files(pack_path, pack_error_ignore_list))

        for content_dir in os.listdir(pack_path):
            content_entity_path = os.path.join(pack_path, content_dir)
            if content_dir in CONTENT_ENTITIES_DIRS:
                pack_entities_validation_results.add(self.run_validation_on_content_entities(content_entity_path,
                                                                                             pack_error_ignore_list))
            else:
                self.ignored_files.add(content_entity_path)

        return all(pack_entities_validation_results)

    def run_validation_on_content_entities(self, content_entity_dir_path, pack_error_ignore_list):
        """Gets non-pack folder and runs validation within it (Scripts, Integrations...)

        Returns:
            bool. true if all files in directory are valid, false otherwise.
        """
        content_entities_validation_results = set()
        for file_name in os.listdir(content_entity_dir_path):
            file_path = os.path.join(content_entity_dir_path, file_name)
            if os.path.isfile(file_path):
                if file_path.endswith('.json') or file_path.endswith('.yml') or file_path.endswith('.md'):
                    content_entities_validation_results.add(self.run_validations_on_file(file_path,
                                                                                         pack_error_ignore_list))
                else:
                    self.ignored_files.add(file_path)

            else:
                content_entities_validation_results.add(self.run_validation_on_package(file_path,
                                                                                       pack_error_ignore_list))

        return all(content_entities_validation_results)

    def run_validation_on_package(self, package_path, pack_error_ignore_list):
        package_entities_validation_results = set()

        for file_name in os.listdir(package_path):
            file_path = os.path.join(package_path, file_name)
            if file_path.endswith('.yml') or file_path.endswith('.md'):
                package_entities_validation_results.add(self.run_validations_on_file(file_path, pack_error_ignore_list))

            else:
                self.ignored_files.add(file_path)

        return all(package_entities_validation_results)

    # flake8: noqa: C901
    def run_validations_on_file(self, file_path, pack_error_ignore_list, is_modified=False,
                                old_file_path=None, modified_files=None, added_files=None):
        """Choose a validator to run for a single file. (i)

        Args:
            modified_files: A set of modified files - used for RN validation
            added_files: A set of added files - used for RN validation
            old_file_path: The old file path for renamed files
            pack_error_ignore_list: A dictionary of all pack ignored errors
            file_path: the file on which to run.
            is_modified: whether the file is modified or added.

        Returns:
            bool. true if file is valid, false otherwise.
        """
        file_type = find_type(file_path)

        if file_type in self.skipped_file_types or file_path.endswith('_unified.yml'):
            self.ignored_files.add(file_path)
            return True
        elif file_type is None:
            error_message, error_code = Errors.file_type_not_supported()
            if self.handle_error(error_message=error_message, error_code=error_code, file_path=file_path,
                                 drop_line=True):
                return False

        if not self.check_only_schema:
            validation_print = f"\nValidating {file_path} as {file_type.value}"
            if self.print_percent:
                validation_print += f' [{self.completion_percentage}%]'
            click.echo(validation_print)

        structure_validator = StructureValidator(file_path, predefined_scheme=file_type,
                                                 ignored_errors=pack_error_ignore_list,
                                                 print_as_warnings=self.print_ignored_errors, tag=self.prev_ver,
                                                 old_file_path=old_file_path, branch_name=self.branch_name,
                                                 is_new_file=not is_modified)

        # schema validation
        if file_type not in {FileType.TEST_PLAYBOOK, FileType.TEST_SCRIPT}:
            click.secho(f'Validating scheme for {file_path}')
            if not structure_validator.is_valid_file():
                return False

        # Passed schema validation
        # if only schema validation is required - stop check here
        if self.check_only_schema:
            return True

        # id_set validation
        if self.id_set_validations and not self.id_set_validations.is_file_valid_in_set(file_path, file_type,
                                                                                        pack_error_ignore_list):
            return False

        # Note: these file are not ignored but there are no additional validators for connections
        if file_type == FileType.CONNECTION:
            return True

        # test playbooks and test scripts are using the same validation.
        elif file_type in {FileType.TEST_PLAYBOOK, FileType.TEST_SCRIPT}:
            return self.validate_test_playbook(structure_validator, pack_error_ignore_list)

        elif file_type == FileType.RELEASE_NOTES:
            if not self.skip_pack_rn_validation:
                return self.validate_release_notes(file_path, added_files, modified_files, pack_error_ignore_list,
                                                   is_modified)

        elif file_type == FileType.README:
            return self.validate_readme(file_path, pack_error_ignore_list)

        elif file_type == FileType.REPORT:
            return self.validate_report(structure_validator, pack_error_ignore_list)

        elif file_type == FileType.PLAYBOOK:
            return self.validate_playbook(structure_validator, pack_error_ignore_list, file_type)

        elif file_type == FileType.INTEGRATION:
            return self.validate_integration(structure_validator, pack_error_ignore_list, is_modified, file_type)

        elif file_type == FileType.SCRIPT:
            return self.validate_script(structure_validator, pack_error_ignore_list, is_modified, file_type)

        elif file_type == FileType.BETA_INTEGRATION:
            return self.validate_beta_integration(structure_validator, pack_error_ignore_list)

        # Validate only images of packs
        elif file_type == FileType.IMAGE:
            return self.validate_image(file_path, pack_error_ignore_list)

        # incident fields and indicator fields are using the same validation.
        elif file_type in (FileType.INCIDENT_FIELD, FileType.INDICATOR_FIELD):
            return self.validate_incident_field(structure_validator, pack_error_ignore_list, is_modified)

        elif file_type == FileType.REPUTATION:
            return self.validate_reputation(structure_validator, pack_error_ignore_list)

        elif file_type == FileType.LAYOUT:
            return self.validate_layout(structure_validator, pack_error_ignore_list)

        elif file_type == FileType.LAYOUTS_CONTAINER:
            return self.validate_layoutscontainer(structure_validator, pack_error_ignore_list)

        elif file_type == FileType.DASHBOARD:
            return self.validate_dashboard(structure_validator, pack_error_ignore_list)

        elif file_type == FileType.INCIDENT_TYPE:
            return self.validate_incident_type(structure_validator, pack_error_ignore_list, is_modified)

        elif file_type == FileType.MAPPER:
            return self.validate_mapper(structure_validator, pack_error_ignore_list)

        elif file_type in (FileType.OLD_CLASSIFIER, FileType.CLASSIFIER):
            return self.validate_classifier(structure_validator, pack_error_ignore_list, file_type)

        elif file_type == FileType.WIDGET:
            return self.validate_widget(structure_validator, pack_error_ignore_list)

        else:
            error_message, error_code = Errors.file_type_not_supported()
            if self.handle_error(error_message=error_message, error_code=error_code, file_path=file_path):
                return False

        return True

    def run_validation_using_git(self):
        """Runs validation on only changed packs/files (g)
        """
        valid_git_setup = self.setup_git_params()
        if not self.no_configuration_prints:
            self.git_config_prints()

        modified_files, added_files, changed_meta_files, old_format_files = \
            self.get_changed_files_from_git()

        validation_results = {valid_git_setup}

        validation_results.add(self.validate_modified_files(modified_files))
        validation_results.add(self.validate_added_files(added_files, modified_files))
        validation_results.add(self.validate_changed_packs_unique_files(modified_files, added_files,
                                                                        changed_meta_files))

        if old_format_files:
            click.secho(f'\n================= Running validation on old format files =================',
                        fg="bright_cyan")
            validation_results.add(self.validate_no_old_format(old_format_files))

        if not self.skip_pack_rn_validation:
            validation_results.add(self.validate_no_duplicated_release_notes(added_files))
            validation_results.add(self.validate_no_missing_release_notes(modified_files, old_format_files,
                                                                          added_files))

        if self.changes_in_schema:
            self.check_only_schema = True
            click.secho(f'\n================= Detected changes in schema - Running validation on all files '
                        f'=================',
                        fg="bright_cyan")
            validation_results.add(self.run_validation_on_all_packs())

        return all(validation_results)

    """ ######################################## Unique Validations ####################################### """

    def validate_readme(self, file_path, pack_error_ignore_list):
        readme_validator = ReadMeValidator(file_path, ignored_errors=pack_error_ignore_list,
                                           print_as_warnings=self.print_ignored_errors)
        return readme_validator.is_valid_file()

    def validate_test_playbook(self, structure_validator, pack_error_ignore_list):
        test_playbook_validator = TestPlaybookValidator(structure_validator=structure_validator,
                                                        ignored_errors=pack_error_ignore_list,
                                                        print_as_warnings=self.print_ignored_errors)
        return test_playbook_validator.is_valid_test_playbook(validate_rn=False)

    def validate_release_notes(self, file_path, added_files, modified_files, pack_error_ignore_list, is_modified):
        pack_name = get_pack_name(file_path)

        # modified existing RN
        if is_modified:
            error_message, error_code = Errors.modified_existing_release_notes(pack_name)
            if self.handle_error(error_message=error_message, error_code=error_code, file_path=file_path):
                return False

        # added new RN to a new pack
        if pack_name in self.new_packs:
            error_message, error_code = Errors.added_release_notes_for_new_pack(pack_name)
            if self.handle_error(error_message=error_message, error_code=error_code, file_path=file_path):
                return False

        if pack_name != 'NonSupported':
            if not added_files:
                added_files = {file_path}

            release_notes_validator = ReleaseNotesValidator(file_path, pack_name=pack_name,
                                                            modified_files=modified_files,
                                                            added_files=added_files,
                                                            ignored_errors=pack_error_ignore_list,
                                                            print_as_warnings=self.print_ignored_errors)
            return release_notes_validator.is_file_valid()

        return True

    def validate_playbook(self, structure_validator, pack_error_ignore_list, file_type):
        playbook_validator = PlaybookValidator(structure_validator, ignored_errors=pack_error_ignore_list,
                                               print_as_warnings=self.print_ignored_errors)

        deprecated_result = self.check_and_validate_deprecated(file_type=file_type,
                                                               file_path=structure_validator.file_path,
                                                               current_file=playbook_validator.current_file,
                                                               is_modified=True,
                                                               is_backward_check=False,
                                                               validator=playbook_validator)
        if deprecated_result is not None:
            return deprecated_result

        return playbook_validator.is_valid_playbook(validate_rn=False,
                                                    id_set_file=self.id_set_file)

    def validate_integration(self, structure_validator, pack_error_ignore_list, is_modified, file_type):
        integration_validator = IntegrationValidator(structure_validator, ignored_errors=pack_error_ignore_list,
                                                     print_as_warnings=self.print_ignored_errors,
                                                     skip_docker_check=self.skip_docker_checks)

        deprecated_result = self.check_and_validate_deprecated(file_type=file_type,
                                                               file_path=structure_validator.file_path,
                                                               current_file=integration_validator.current_file,
                                                               is_modified=is_modified,
                                                               is_backward_check=self.is_backward_check,
                                                               validator=integration_validator)
        if deprecated_result is not None:
            return deprecated_result

        if is_modified and self.is_backward_check:
            return all([integration_validator.is_valid_file(validate_rn=False, skip_test_conf=self.skip_conf_json),
                        integration_validator.is_backward_compatible()])
        else:
            return integration_validator.is_valid_file(validate_rn=False, skip_test_conf=self.skip_conf_json)

    def validate_script(self, structure_validator, pack_error_ignore_list, is_modified, file_type):
        script_validator = ScriptValidator(structure_validator, ignored_errors=pack_error_ignore_list,
                                           print_as_warnings=self.print_ignored_errors,
                                           skip_docker_check=self.skip_docker_checks)

        deprecated_result = self.check_and_validate_deprecated(file_type=file_type,
                                                               file_path=structure_validator.file_path,
                                                               current_file=script_validator.current_file,
                                                               is_modified=is_modified,
                                                               is_backward_check=self.is_backward_check,
                                                               validator=script_validator)
        if deprecated_result is not None:
            return deprecated_result

        if is_modified and self.is_backward_check:
            return all([script_validator.is_valid_file(validate_rn=False),
                        script_validator.is_backward_compatible()])
        else:
            return script_validator.is_valid_file(validate_rn=False)

    def validate_beta_integration(self, structure_validator, pack_error_ignore_list):
        integration_validator = IntegrationValidator(structure_validator, ignored_errors=pack_error_ignore_list,
                                                     print_as_warnings=self.print_ignored_errors,
                                                     skip_docker_check=self.skip_docker_checks)
        return integration_validator.is_valid_beta_integration()

    def validate_image(self, file_path, pack_error_ignore_list):
        image_validator = ImageValidator(file_path, ignored_errors=pack_error_ignore_list,
                                         print_as_warnings=self.print_ignored_errors)
        return image_validator.is_valid()

    def validate_report(self, structure_validator, pack_error_ignore_list):
        report_validator = ReportValidator(structure_validator=structure_validator,
                                           ignored_errors=pack_error_ignore_list,
                                           print_as_warnings=self.print_ignored_errors)
        return report_validator.is_valid_file(validate_rn=False)

    def validate_incident_field(self, structure_validator, pack_error_ignore_list, is_modified):
        incident_field_validator = IncidentFieldValidator(structure_validator, ignored_errors=pack_error_ignore_list,
                                                          print_as_warnings=self.print_ignored_errors)
        if is_modified and self.is_backward_check:
            return all([incident_field_validator.is_valid_file(validate_rn=False),
                        incident_field_validator.is_backward_compatible()])
        else:
            return incident_field_validator.is_valid_file(validate_rn=False)

    def validate_reputation(self, structure_validator, pack_error_ignore_list):
        reputation_validator = ReputationValidator(structure_validator, ignored_errors=pack_error_ignore_list,
                                                   print_as_warnings=self.print_ignored_errors)
        return reputation_validator.is_valid_file(validate_rn=False)

    def validate_layout(self, structure_validator, pack_error_ignore_list):
        layout_validator = LayoutValidator(structure_validator, ignored_errors=pack_error_ignore_list,
                                           print_as_warnings=self.print_ignored_errors)
        return layout_validator.is_valid_layout(validate_rn=False, id_set_file=self.id_set_file,
                                                is_circle=self.is_circle)

    def validate_layoutscontainer(self, structure_validator, pack_error_ignore_list):
        layout_validator = LayoutsContainerValidator(structure_validator, ignored_errors=pack_error_ignore_list,
                                                     print_as_warnings=self.print_ignored_errors)
        return layout_validator.is_valid_layout(validate_rn=False, id_set_file=self.id_set_file,
                                                is_circle=self.is_circle)

    def validate_dashboard(self, structure_validator, pack_error_ignore_list):
        dashboard_validator = DashboardValidator(structure_validator, ignored_errors=pack_error_ignore_list,
                                                 print_as_warnings=self.print_ignored_errors)
        return dashboard_validator.is_valid_dashboard(validate_rn=False)

    def validate_incident_type(self, structure_validator, pack_error_ignore_list, is_modified):
        incident_type_validator = IncidentTypeValidator(structure_validator, ignored_errors=pack_error_ignore_list,
                                                        print_as_warnings=self.print_ignored_errors)
        if is_modified and self.is_backward_check:
            return all([incident_type_validator.is_valid_incident_type(validate_rn=False),
                        incident_type_validator.is_backward_compatible()])
        else:
            return incident_type_validator.is_valid_incident_type(validate_rn=False)

    def validate_mapper(self, structure_validator, pack_error_ignore_list):
        mapper_validator = MapperValidator(structure_validator, ignored_errors=pack_error_ignore_list,
                                           print_as_warnings=self.print_ignored_errors)
        return mapper_validator.is_valid_mapper(validate_rn=False, id_set_file=self.id_set_file,
                                                is_circle=self.is_circle)

    def validate_classifier(self, structure_validator, pack_error_ignore_list, file_type):
        if file_type == FileType.CLASSIFIER:
            new_classifier_version = True

        else:
            new_classifier_version = False

        classifier_validator = ClassifierValidator(structure_validator, new_classifier_version=new_classifier_version,
                                                   ignored_errors=pack_error_ignore_list,
                                                   print_as_warnings=self.print_ignored_errors)
        return classifier_validator.is_valid_classifier(validate_rn=False,
                                                        id_set_file=self.id_set_file,
                                                        is_circle=self.is_circle)

    def validate_widget(self, structure_validator, pack_error_ignore_list):
        widget_validator = WidgetValidator(structure_validator, ignored_errors=pack_error_ignore_list,
                                           print_as_warnings=self.print_ignored_errors)
        return widget_validator.is_valid_file(validate_rn=False)

    def validate_pack_unique_files(self, pack_path: str, pack_error_ignore_list: dict, id_set_path=None,
                                   should_version_raise=False) -> bool:
        """
        Runs validations on the following pack files:
        * .secret-ignore: Validates that the file exist and that the file's secrets can be parsed as a list delimited by '\n'
        * .pack-ignore: Validates that the file exists and that all regexes in it can be compiled
        * README.md file: Validates that the file exists
        * 2.pack_metadata.json: Validates that the file exists and that it has a valid structure
        Runs validation on the pack dependencies
        Args:
            id_set_path (str): Path of the id_set. Optional.
            should_version_raise: Whether we should check if the version of the metadata was raised
            pack_error_ignore_list: A dictionary of all pack ignored errors
            pack_path: A path to a pack
        """
        print(f'\nValidating {pack_path} unique pack files')

        pack_unique_files_validator = PackUniqueFilesValidator(pack=os.path.basename(pack_path),
                                                               pack_path=pack_path,
                                                               ignored_errors=pack_error_ignore_list,
                                                               print_as_warnings=self.print_ignored_errors,
                                                               should_version_raise=should_version_raise,
                                                               validate_dependencies=not self.skip_dependencies,
                                                               id_set_path=id_set_path,
                                                               private_repo=self.is_external_repo,
                                                               skip_id_set_creation=self.skip_id_set_creation)
        pack_errors = pack_unique_files_validator.validate_pack_unique_files()
        if pack_errors:
            click.secho(pack_errors, fg="bright_red")
            return False

        return True

    def validate_modified_files(self, modified_files):
        click.secho(f'\n================= Running validation on modified files =================',
                    fg="bright_cyan")
        valid_files = set()
        for file_path in modified_files:
            # handle renamed files
            if isinstance(file_path, tuple):
                old_file_path = file_path[0]
                file_path = file_path[1]

            else:
                old_file_path = None

            pack_name = get_pack_name(file_path)
            valid_files.add(self.run_validations_on_file(file_path, self.get_error_ignore_list(pack_name),
                                                         is_modified=True, old_file_path=old_file_path))
        return all(valid_files)

    def validate_added_files(self, added_files, modified_files):
        click.secho(f'\n================= Running validation on newly added files =================',
                    fg="bright_cyan")

        valid_files = set()
        for file_path in added_files:
            pack_name = get_pack_name(file_path)
            valid_files.add(self.run_validations_on_file(file_path, self.get_error_ignore_list(pack_name),
                                                         is_modified=False, modified_files=modified_files,
                                                         added_files=added_files))
        return all(valid_files)

    @staticmethod
    def should_raise_pack_version(pack: str) -> bool:
        """
        Args:
            pack: The pack name.

        Returns: False if pack is in IGNORED_PACK_NAMES else True.

        """
        return pack not in IGNORED_PACK_NAMES

    def validate_changed_packs_unique_files(self, modified_files, added_files, changed_meta_files):
        click.secho(f'\n================= Running validation on changed pack unique files =================',
                    fg="bright_cyan")
        valid_pack_files = set()

        added_packs = get_pack_names_from_files(added_files)
        modified_packs = get_pack_names_from_files(modified_files)
        changed_meta_packs = get_pack_names_from_files(changed_meta_files)

        packs_that_should_have_version_raised = self.get_packs_that_should_have_version_raised(modified_files,
                                                                                               added_files)

        changed_packs = modified_packs.union(added_packs).union(changed_meta_packs)

        for pack in changed_packs:
            raise_version = False
            pack_path = tools.pack_name_to_path(pack)
            if pack in packs_that_should_have_version_raised:
                raise_version = self.should_raise_pack_version(pack)
            valid_pack_files.add(self.validate_pack_unique_files(
                pack_path, self.get_error_ignore_list(pack), should_version_raise=raise_version,
                id_set_path=self.id_set_path))

        return all(valid_pack_files)

    def validate_no_old_format(self, old_format_files):
        """ Validate there are no files in the old format(unified yml file for the code and configuration).

        Args:
            old_format_files(set): file names which are in the old format.
        """
        handle_error = True
        for file_path in old_format_files:
            click.echo(f"Validating old-format file {file_path}")
            yaml_data = get_yaml(file_path)
            # we only fail on old format if no toversion (meaning it is latest) or if the ynl is not deprecated.
            if 'toversion' not in yaml_data and not yaml_data.get('deprecated'):
                error_message, error_code = Errors.invalid_package_structure(file_path)
                if self.handle_error(error_message, error_code, file_path=file_path):
                    handle_error = False
        return handle_error

    def validate_no_duplicated_release_notes(self, added_files):
        """Validated that among the added files - there are no duplicated RN for the same pack.

        Args:
            added_files(set): The added files

        Returns:
            bool. True if no duplications found, false otherwise
        """
        click.secho(f'\n================= Verifying no duplicated release notes =================',
                    fg="bright_cyan")
        added_rn = set()
        for file in added_files:
            if find_type(file) == FileType.RELEASE_NOTES:
                pack_name = get_pack_name(file)
                if pack_name not in added_rn:
                    added_rn.add(pack_name)
                else:
                    error_message, error_code = Errors.multiple_release_notes_files()
                    if self.handle_error(error_message, error_code, file_path=pack_name):
                        return False

        click.secho("\nNo duplicated release notes found.\n", fg="bright_green")
        return True

    def validate_no_missing_release_notes(self, modified_files, old_format_files, added_files):
        """Validate that there are no missing RN for changed files

        Args:
            modified_files (set): a set of modified files.
            old_format_files (set): a set of old format files that were changed.
            added_files (set): a set of files that were added.

        Returns:
            bool. True if no missing RN found, False otherwise
        """
        click.secho("\n================= Checking for missing release notes =================\n", fg="bright_cyan")
        packs_that_should_have_new_rn_api_module_related: set = set()
        # existing packs that have files changed (which are not RN, README nor test files) - should have new RN
        changed_files = modified_files.union(old_format_files).union(added_files)
        packs_that_should_have_new_rn = get_pack_names_from_files(changed_files,
                                                                  skip_file_types={FileType.RELEASE_NOTES,
                                                                                   FileType.README,
                                                                                   FileType.TEST_PLAYBOOK,
                                                                                   FileType.TEST_SCRIPT,
                                                                                   FileType.DOC_IMAGE})
        if API_MODULES_PACK in packs_that_should_have_new_rn:
            api_module_set = get_api_module_ids(changed_files)
            integrations = get_api_module_integrations_set(api_module_set,
                                                           self.id_set_file.get('integrations', []))
            packs_that_should_have_new_rn_api_module_related = set(map(lambda integration: integration.get('pack'),
                                                                       integrations))
            packs_that_should_have_new_rn = packs_that_should_have_new_rn.union(
                packs_that_should_have_new_rn_api_module_related)

            # APIModules pack is without a version and should not have RN
            packs_that_should_have_new_rn.remove(API_MODULES_PACK)

        # new packs should not have RN
        packs_that_should_have_new_rn = packs_that_should_have_new_rn - self.new_packs

        packs_that_have_new_rn = self.get_packs_with_added_release_notes(added_files)

        packs_that_have_missing_rn = packs_that_should_have_new_rn.difference(packs_that_have_new_rn)

        if len(packs_that_have_missing_rn) > 0:
            is_valid = set()
            for pack in packs_that_have_missing_rn:
                # # ignore RN in NonSupported pack
                if 'NonSupported' in pack:
                    continue
                ignored_errors_list = self.get_error_ignore_list(pack)
                if pack in packs_that_should_have_new_rn_api_module_related:
                    error_message, error_code = Errors.missing_release_notes_for_pack(API_MODULES_PACK)
                else:
                    error_message, error_code = Errors.missing_release_notes_for_pack(pack)
                if not BaseValidator(ignored_errors=ignored_errors_list,
                                     print_as_warnings=self.print_ignored_errors).handle_error(
                        error_message, error_code, file_path=os.path.join(PACKS_DIR, pack)):
                    is_valid.add(True)

                else:
                    is_valid.add(False)

            return all(is_valid)

        else:
            click.secho("No missing release notes found.\n", fg="bright_green")
            return True

    """ ######################################## Git Tools and filtering ####################################### """

    def setup_git_params(self):
        """Setting up the git relevant params"""
        self.branch_name = self.git_util.get_current_working_branch() if (self.git_util and not self.branch_name) \
            else self.branch_name

        # if running on release branch check against last release.
        if self.branch_name.startswith('19.') or self.branch_name.startswith('20.'):
            self.skip_pack_rn_validation = True

            self.prev_ver = get_content_release_identifier(self.branch_name)

            # when running against git while on release branch - show errors but don't fail the validation
            if self.branch_name.startswith('20.'):
                self.always_valid = True

        # on master don't check RN
        elif self.branch_name == 'master':
            self.skip_pack_rn_validation = True
            error_message, error_code = Errors.running_on_master_with_git()
            if self.handle_error(error_message, error_code, file_path='General', warning=not self.is_external_repo):
                return False
        return True

    def git_config_prints(self):
        click.secho(f'\n================= Running validation on branch {self.branch_name} =================',
                    fg="bright_cyan")
        if not self.no_configuration_prints:
            click.echo(f"Validating against {self.prev_ver}")

            if self.is_circle:
                click.echo("Running only on committed files")

            elif self.staged:
                click.echo("Running only on staged files")

            else:
                click.echo("Running on committed and staged files")

    def get_changed_files_from_git(self):
        """Get the added and modified after file filtration to only relevant files for validate"""
        # get files from git by status identification against prev-ver
        modified_files = self.git_util.modified_files(prev_ver=self.prev_ver,
                                                      committed_only=self.is_circle, staged_only=self.staged)
        added_files = self.git_util.added_files(prev_ver=self.prev_ver, committed_only=self.is_circle,
                                                staged_only=self.staged)
        renamed_files = self.git_util.renamed_files(prev_ver=self.prev_ver, committed_only=self.is_circle,
                                                    staged_only=self.staged)

        # filter files only to relevant files
        filtered_modified, old_format_files = self.filter_to_relevant_files(modified_files)
        filtered_renamed, _ = self.filter_to_relevant_files(renamed_files)
        filtered_modified = filtered_modified.union(filtered_renamed)
        filtered_added, _ = self.filter_to_relevant_files(added_files)

        # extract metadata files from the recognised changes
        changed_meta = self.pack_metadata_extraction(modified_files, added_files, renamed_files)

        return filtered_modified, filtered_added, changed_meta, old_format_files

    def pack_metadata_extraction(self, modified_files, added_files, renamed_files):
        """Extract pack metadata files from the modified and added files

        Return all modified metadata file paths
        and get all newly added packs from the added metadata files."""
        changed_metadata_files = set()
        for path in modified_files.union(renamed_files):
            file_path = str(path[1]) if isinstance(path, tuple) else str(path)

            if file_path.endswith(PACKS_PACK_META_FILE_NAME):
                changed_metadata_files.add(file_path)

        for path in added_files:
            if str(path).endswith(PACKS_PACK_META_FILE_NAME):
                self.new_packs.add(get_pack_name(str(path)))

        return changed_metadata_files

    def filter_to_relevant_files(self, file_set):
        """Goes over file set and returns only a filtered set of only files relevant for validation"""
        filtered_set: set = set()
        old_format_files: set = set()
        for path in file_set:
            old_path = None
            if isinstance(path, tuple):
                file_path = str(path[1])
                old_path = str(path[0])

            else:
                file_path = str(path)

            try:
                formatted_path = self.format_file_path(file_path, old_path, old_format_files)
                if formatted_path:
                    filtered_set.add(formatted_path)

            # handle a case where a file was deleted locally though recognised as added against master.
            except FileNotFoundError:
                if file_path not in self.ignored_files:
                    self.ignored_files.add(file_path)

        return filtered_set, old_format_files

    def format_file_path(self, file_path, old_path, old_format_files):
        """Determines if a file is relevant for validation and create any modification to the file_path if needed"""
        file_type = find_type(file_path)

        # ignore unrecognized file types, pack metadata, doc data and test_data
        if not file_type or file_type == FileType.PACK_METADATA or any(test_dir in str(file_path) for
                                                                       test_dir in TESTS_AND_DOC_DIRECTORIES):
            self.ignored_files.add(file_path)
            return None

        # redirect non-test code files to the associated yml file
        if file_type in [FileType.PYTHON_FILE, FileType.POWERSHELL_FILE]:
            if not (str(file_path).endswith('_test.py') or str(file_path).endswith('.Tests.ps1')):
                file_path = file_path.replace('.py', '.yml').replace('.ps1', '.yml')

                if old_path:
                    old_path = old_path.replace('.py', '.yml').replace('.ps1', ',yml')
            else:
                return None

        # check for old file format
        if self.is_old_file_format(file_path, file_type):
            old_format_files.add(file_path)
            return None

        # if renamed file - return a tuple
        if old_path:
            return old_path, file_path

        # else return the file path
        else:
            return file_path

    """ ######################################## Validate Tools ############################################### """

    @staticmethod
    def create_ignored_errors_list(errors_to_check):
        ignored_error_list = []
        all_errors = ERROR_CODE.values()
        for error_code in all_errors:
            error_type = error_code[:2]
            if error_code not in errors_to_check and error_type not in errors_to_check:
                ignored_error_list.append(error_code)

        return ignored_error_list

    @staticmethod
    def get_allowed_ignored_errors_from_list(error_list):
        allowed_ignore_list = []
        for error in error_list:
            if error in ALLOWED_IGNORE_ERRORS:
                allowed_ignore_list.append(error)

        return allowed_ignore_list

    def add_ignored_errors_to_list(self, config, section, key, ignored_errors_list):
        if key == 'ignore':
            ignored_errors_list.extend(self.get_allowed_ignored_errors_from_list(str(config[section][key]).split(',')))

        if key in PRESET_ERROR_TO_IGNORE:
            ignored_errors_list.extend(PRESET_ERROR_TO_IGNORE.get(key))

        if key in PRESET_ERROR_TO_CHECK:
            ignored_errors_list.extend(
                self.create_ignored_errors_list(PRESET_ERROR_TO_CHECK.get(key)))

    def get_error_ignore_list(self, pack_name):
        ignored_errors_list: dict = {}
        if pack_name:
            pack_ignore_path = get_pack_ignore_file_path(pack_name)

            if os.path.isfile(pack_ignore_path):
                try:
                    config = ConfigParser(allow_no_value=True)
                    config.read(pack_ignore_path)

                    # create file specific ignored errors list
                    for section in config.sections():
                        if section.startswith("file:"):
                            file_name = section[5:]
                            ignored_errors_list[file_name] = []
                            for key in config[section]:
                                self.add_ignored_errors_to_list(config, section, key, ignored_errors_list[file_name])

                except MissingSectionHeaderError:
                    pass

        return ignored_errors_list

    def get_content_release_identifier(self) -> Optional[str]:
        return tools.get_content_release_identifier(self.branch_name)

    @staticmethod
    def is_old_file_format(file_path, file_type):
        file_yml = get_yaml(file_path)
        # check for unified integration
        if file_type == FileType.INTEGRATION and file_yml.get('script', {}).get('script', '-') not in ['-', '']:
            if file_yml.get('script', {}).get('type', 'javascript') != 'python':
                return False
            return True

        # check for unified script
        if file_type == FileType.SCRIPT and file_yml.get('script', '-') not in ['-', '']:
            if file_yml.get('type', 'javascript') != 'python':
                return False
            return True
        return False

    @staticmethod
    def get_packs_with_added_release_notes(added_files):
        added_rn = set()
        for file in added_files:
            if find_type(path=file) == FileType.RELEASE_NOTES:
                added_rn.add(get_pack_name(file))

        return added_rn

    def print_ignored_errors_report(self, print_ignored_errors):
        if print_ignored_errors:
            all_ignored_errors = '\n'.join(FOUND_FILES_AND_IGNORED_ERRORS)
            click.secho(f"\n=========== Found ignored errors "
                        f"in the following files ===========\n\n{all_ignored_errors}",
                        fg="yellow")

    def print_ignored_files_report(self, print_ignored_files):
        if print_ignored_files:
            all_ignored_files = '\n'.join(list(self.ignored_files))
            click.secho(f"\n=========== Ignored the following files ===========\n\n{all_ignored_files}",
                        fg="yellow")

    def get_packs_that_should_have_version_raised(self, modified_files, added_files):
        # modified packs (where the change is not test-playbook, test-script, readme, metadata file or release notes)
        modified_packs_that_should_have_version_raised = get_pack_names_from_files(modified_files, skip_file_types={
            FileType.RELEASE_NOTES, FileType.README, FileType.TEST_PLAYBOOK,
            FileType.TEST_SCRIPT, FileType.PACK_METADATA
        })

        # also existing packs with added files which are not test-playbook, test-script readme or release notes
        # should have their version raised
        modified_packs_that_should_have_version_raised = modified_packs_that_should_have_version_raised.union(
            get_pack_names_from_files(added_files, skip_file_types={
                FileType.RELEASE_NOTES, FileType.README, FileType.TEST_PLAYBOOK,
                FileType.TEST_SCRIPT}) - self.new_packs)

        return modified_packs_that_should_have_version_raised

    @staticmethod
    def get_packs(changed_files):
        packs = set()
        for changed_file in changed_files:
            if isinstance(changed_file, tuple):
                changed_file = changed_file[1]
            pack = get_pack_name(changed_file)
            if pack:
                packs.add(pack)

        return packs

    @staticmethod
    def get_id_set_file(skip_id_set_creation, id_set_path):
        """

        Args:
            skip_id_set_creation (bool): whether should skip id set validation or not
            this will also determine whether a new id_set can be created by validate.
            id_set_path (str): id_set.json path file

        Returns:
            str: is_set file path
        """
        id_set = {}
        if not os.path.isfile(id_set_path):
            if not skip_id_set_creation:
                id_set = IDSetCreator(print_logs=False).create_id_set()
        else:
            id_set = open_id_set_file(id_set_path)
        return id_set

    def check_and_validate_deprecated(self, file_type, file_path, current_file, is_modified, is_backward_check,
                                      validator):
        """If file is deprecated, validate it. Return None otherwise.

        Files with 'deprecated: true' or 'toversion < OLDEST_SUPPORTED_VERSION' fields are considered deprecated.

        Args:
            file_type: (FileType) Type of file to validate.
            file_path: (str) file path to validate.
            current_file: (dict) file in json format to validate.
            is_modified: (boolean) for whether the file was modified.
            is_backward_check: (boolean) for whether to preform backwards compatibility validation.
            validator: (ContentEntityValidator) validator object to run backwards compatibility validation from.

        Returns:
            True if current_file is deprecated and valid.
            False if current_file is deprecated and invalid.
            None if current_file is not deprecated.
        """
        if file_type == FileType.PLAYBOOK:
            is_deprecated = "hidden" in current_file and current_file["hidden"]
        else:
            is_deprecated = "deprecated" in current_file and current_file["deprecated"]

        toversion_is_old = "toversion" in current_file and \
                           version.parse(current_file.get("toversion", "99.99.99")) < \
                           version.parse(OLDEST_SUPPORTED_VERSION)

        if is_deprecated or toversion_is_old:
            click.echo(f"Validating deprecated file: {file_path}")

            is_valid_as_deprecated = True
            if hasattr(validator, "is_valid_as_deprecated"):
                is_valid_as_deprecated = validator.is_valid_as_deprecated()

            if is_modified and is_backward_check:
                return all([is_valid_as_deprecated, validator.is_backward_compatible()])

            self.ignored_files.add(file_path)
            if self.print_ignored_files:
                click.echo(f"Skipping validation for: {file_path}")

            return is_valid_as_deprecated
        return None
