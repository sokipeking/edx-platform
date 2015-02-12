import json
import logging

from celery.task import task
from django.dispatch import receiver
from model_utils.models import TimeStampedModel
from opaque_keys.edx.locator import CourseLocator
from xmodule.modulestore.django import modulestore, SignalHandler

from util.models import CompressedTextField
from xmodule_django.models import CourseKeyField


logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


class CourseStructure(TimeStampedModel):
    course_id = CourseKeyField(max_length=255, db_index=True, unique=True, verbose_name='Course ID')

    # Right now the only thing we do with the structure doc is store it and
    # send it on request. If we need to store a more complex data model later,
    # we can do so and build a migration. The only problem with a normalized
    # data model for this is that it will likely involve hundreds of rows, and
    # we'd have to be careful about caching.
    structure_json = CompressedTextField(verbose_name='Structure JSON', blank=True, null=True)

    @property
    def structure(self):
        if self.structure_json:
            return json.loads(self.structure_json)
        return None


def generate_course_structure(course_key):
    """
    Generates a course structure dictionary for the specified course.
    """
    course = modulestore().get_course(course_key, depth=None)
    blocks_stack = [course]
    blocks_dict = {}
    while blocks_stack:
        curr_block = blocks_stack.pop()
        children = curr_block.get_children() if curr_block.has_children else []
        blocks_dict[unicode(curr_block.scope_ids.usage_id)] = {
            "usage_key": unicode(curr_block.scope_ids.usage_id),
            "block_type": curr_block.category,
            "display_name": curr_block.display_name,
            "graded": curr_block.graded,
            "format": curr_block.format,
            "children": [unicode(child.scope_ids.usage_id) for child in children]
        }
        blocks_stack.extend(children)
    return {
        "root": unicode(course.scope_ids.usage_id),
        "blocks": blocks_dict
    }


@receiver(SignalHandler.course_published)
def listen_for_course_publish(sender, course_key, **kwargs):
    update_course_structure.delay(course_key)


@task()
def update_course_structure(course_key):
    """
    Regenerates and updates the course structure (in the database) for the specified course.
    """
    if not isinstance(course_key, CourseLocator):
        logger.error('update_course_structure requires a CourseLocator. Given %s.', type(course_key))
        return

    try:
        structure = generate_course_structure(course_key)
    except Exception as e:
        logger.error('An error occurred while generating course structure: %s', e)
        raise

    structure_json = json.dumps(structure)

    cs, created = CourseStructure.objects.get_or_create(
        course_id=course_key,
        defaults={'structure_json': structure_json}
    )

    if not created:
        cs.structure_json = structure_json
        cs.save()

    return cs
