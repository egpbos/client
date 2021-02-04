"""
Wandb has special data types for logging rich visualizations.

All of the special data types are subclasses of WBValue. All of the data types
serialize to JSON, since that is what wandb uses to save the objects locally
and upload them to the W&B server.
"""

from __future__ import print_function

import base64
import binascii
import codecs
import json
import logging
import numbers
import os
import pprint
import sys
import warnings

import six
from six.moves.collections_abc import Sequence
import wandb
from wandb import util
from wandb.compat import tempfile
from wandb.util import has_num

_PY3 = sys.version_info.major == 3 and sys.version_info.minor >= 6

if _PY3:
    from wandb.sdk.interface import _dtypes
    from wandb.sdk.data_types import (
        WBValue,
        Histogram,
        Media,
        BatchableMedia,
        Object3D,
        Molecule,
        Html,
        Video,
    )
else:
    from wandb.sdk_py27.interface import _dtypes
    from wandb.sdk_py27.data_types import (
        WBValue,
        Histogram,
        Media,
        BatchableMedia,
        Object3D,
        Molecule,
        Html,
        Video,
    )

__all__ = [
    "WBValue",
    "Histogram",
    "Media",
    "BatchableMedia",
    "Object3D",
    "Molecule",
    "Html",
    "Video",
]


def _safe_sdk_import():
    """Safely imports sdks respecting python version"""

    if _PY3:
        from wandb.sdk import wandb_run
        from wandb.sdk import wandb_artifacts
    else:
        from wandb.sdk_py27 import wandb_run
        from wandb.sdk_py27 import wandb_artifacts

    return wandb_run, wandb_artifacts


# Get rid of cleanup warnings in Python 2.7.
warnings.filterwarnings(
    "ignore", "Implicitly cleaning up", RuntimeWarning, "wandb.compat.tempfile"
)

# Staging directory so we can encode raw data into files, then hash them before
# we put them into the Run directory to be uploaded.
MEDIA_TMP = tempfile.TemporaryDirectory("wandb-media")

DATA_FRAMES_SUBDIR = os.path.join("media", "data_frames")


class Table(Media):
    """This is a table designed to display sets of records.

    Arguments:
        columns: ([str]) Names of the columns in the table.
            Defaults to ["Input", "Output", "Expected"].
        data: (array) 2D Array of values that will be displayed as strings.
        dataframe: (pandas.DataFrame) DataFrame object used to create the table.
            When set, the other arguments are ignored.
        optional (Union[bool,List[bool]]): If None values are allowed. Singular bool
            applies to all columns. A list of bool values applies to each respective column.
            Default to True.
        allow_mixed_types (bool): Determines if columns are allowed to have mixed types (disables type validation). Defaults to False
    """

    MAX_ROWS = 10000
    MAX_ARTIFACT_ROWS = 200000
    artifact_type = "table"

    def __init__(
        self,
        columns=None,
        data=None,
        rows=None,
        dataframe=None,
        dtype=None,
        optional=True,
        allow_mixed_types=False,
    ):
        """rows is kept for legacy reasons, we use data to mimic the Pandas api"""
        super(Table, self).__init__()
        if allow_mixed_types:
            dtype = _dtypes.AnyType

        # This is kept for legacy reasons (tss: personally, I think we should remove this)
        if columns is None:
            columns = ["Input", "Output", "Expected"]

        # Explicit dataframe option
        if dataframe is not None:
            self._init_from_dataframe(dataframe, columns, optional, dtype)
        else:
            # Expected pattern
            if data is not None:
                if util.is_numpy_array(data):
                    self._init_from_ndarray(data, columns, optional, dtype)
                elif util.is_pandas_data_frame(data):
                    self._init_from_dataframe(data, columns, optional, dtype)
                else:
                    self._init_from_list(data, columns, optional, dtype)

            # legacy
            elif rows is not None:
                self._init_from_list(rows, columns, optional, dtype)

            # Default empty case
            else:
                self._init_from_list([], columns, optional, dtype)

    @staticmethod
    def _assert_valid_columns(columns):
        valid_col_types = [str, int]
        if sys.version_info.major < 3:
            valid_col_types.append(unicode)  # noqa: F821 (unicode is in py2)
        assert type(columns) is list, "columns argument expects a `list` object"
        assert len(columns) == 0 or all(
            [type(col) in valid_col_types for col in columns]
        ), "columns argument expects list of strings or ints"

    def _init_from_list(self, data, columns, optional=True, dtype=None):
        assert type(data) is list, "data argument expects a `list` object"
        self.data = []
        self._assert_valid_columns(columns)
        self.columns = columns
        self._make_column_types(dtype, optional)
        for row in data:
            self.add_data(*row)

    def _init_from_ndarray(self, ndarray, columns, optional=True, dtype=None):
        assert util.is_numpy_array(
            ndarray
        ), "ndarray argument expects a `numpy.ndarray` object"
        self.data = []
        self._assert_valid_columns(columns)
        self.columns = columns
        self._make_column_types(dtype, optional)
        for row in ndarray.tolist():
            self.add_data(*row)

    def _init_from_dataframe(self, dataframe, columns, optional=True, dtype=None):
        assert util.is_pandas_data_frame(
            dataframe
        ), "dataframe argument expects a `pandas.core.frame.DataFrame` object"
        self.data = []
        self.columns = list(dataframe.columns)
        self._make_column_types(dtype, optional)
        for row in range(len(dataframe)):
            self.add_data(*tuple(dataframe[col].values[row] for col in self.columns))

    def _make_column_types(self, dtype=None, optional=True):
        if dtype is None:
            dtype = _dtypes.UnknownType()

        if optional.__class__ != list:
            optional = [optional for _ in range(len(self.columns))]

        if dtype.__class__ != list:
            dtype = [dtype for _ in range(len(self.columns))]

        self._column_types = _dtypes.DictType({})
        for col_name, opt, dt in zip(self.columns, optional, dtype):
            self.cast(col_name, dt, opt)

    def cast(self, col_name, dtype, optional=False):
        wbtype = _dtypes.TypeRegistry.type_from_dtype(dtype)
        if optional:
            wbtype = _dtypes.OptionalType(wbtype)
        col_ndx = self.columns.index(col_name)
        for row in self.data:
            result_type = wbtype.assign(row[col_ndx])
            if isinstance(result_type, _dtypes.InvalidType):
                raise TypeError(
                    "Existing data {}, of type {} cannot be cast to {}".format(
                        row[col_ndx],
                        _dtypes.TypeRegistry.type_of(row[col_ndx]),
                        wbtype,
                    )
                )
            wbtype = result_type
        self._column_types.params["type_map"][col_name] = wbtype
        return wbtype

    def __ne__(self, other):
        return not self.__eq__(other)

    def __eq__(self, other):
        if (
            not isinstance(other, Table)
            or len(self.data) != len(other.data)
            or self.columns != other.columns
            or self._column_types != other._column_types
        ):
            return False

        for row_ndx in range(len(self.data)):
            for col_ndx in range(len(self.data[row_ndx])):
                if self.data[row_ndx][col_ndx] != other.data[row_ndx][col_ndx]:
                    return False

        return True

    def add_row(self, *row):
        logging.warning("add_row is deprecated, use add_data")
        self.add_data(*row)

    def add_data(self, *data):
        """Add a row of data to the table. Argument length should match column length"""
        if len(data) != len(self.columns):
            raise ValueError(
                "This table expects {} columns: {}".format(
                    len(self.columns), self.columns
                )
            )
        self._validate_data(data)
        self.data.append(list(data))

    def _validate_data(self, data):
        incoming_data_dict = {
            col_key: data[ndx] for ndx, col_key in enumerate(self.columns)
        }
        current_type = self._column_types
        result_type = current_type.assign(incoming_data_dict)
        if isinstance(result_type, _dtypes.InvalidType):
            raise TypeError(
                "Data row contained incompatible types:\n{}".format(
                    current_type.explain(incoming_data_dict)
                )
            )
        self._column_types = result_type

    def _to_table_json(self, max_rows=None):
        # seperate method for testing
        if max_rows is None:
            max_rows = Table.MAX_ROWS
        if len(self.data) > max_rows:
            logging.warning("Truncating wandb.Table object to %i rows." % max_rows)
        return {"columns": self.columns, "data": self.data[:max_rows]}

    def bind_to_run(self, *args, **kwargs):
        data = self._to_table_json()
        tmp_path = os.path.join(MEDIA_TMP.name, util.generate_id() + ".table.json")
        data = numpy_arrays_to_lists(data)
        util.json_dump_safer(data, codecs.open(tmp_path, "w", encoding="utf-8"))
        self._set_file(tmp_path, is_tmp=True, extension=".table.json")
        super(Table, self).bind_to_run(*args, **kwargs)

    @classmethod
    def get_media_subdir(cls):
        return os.path.join("media", "table")

    @classmethod
    def from_json(cls, json_obj, source_artifact):
        data = []
        for row in json_obj["data"]:
            row_data = []
            for item in row:
                cell = item
                if isinstance(item, dict):
                    obj = WBValue.init_from_json(item, source_artifact)
                    if obj is not None:
                        cell = obj
                row_data.append(cell)
            data.append(row_data)

        new_obj = cls(columns=json_obj["columns"], data=data)

        if json_obj.get("column_types") is not None:
            new_obj._column_types = _dtypes.TypeRegistry.type_from_dict(
                json_obj["column_types"], source_artifact
            )

        return new_obj

    def to_json(self, run_or_artifact):
        json_dict = super(Table, self).to_json(run_or_artifact)
        wandb_run, wandb_artifacts = _safe_sdk_import()

        if isinstance(run_or_artifact, wandb_run.Run):
            json_dict.update(
                {
                    "_type": "table-file",
                    "ncols": len(self.columns),
                    "nrows": len(self.data),
                }
            )

        elif isinstance(run_or_artifact, wandb_artifacts.Artifact):
            for column in self.columns:
                if isinstance(column, six.string_types) and "." in column:
                    raise ValueError(
                        "invalid column name: {} - tables added to artifacts must not contain periods.".format(
                            column
                        )
                    )
            artifact = run_or_artifact
            mapped_data = []
            data = self._to_table_json(Table.MAX_ARTIFACT_ROWS)["data"]

            def json_helper(val):
                if isinstance(val, WBValue):
                    return val.to_json(artifact)
                elif val.__class__ == dict:
                    res = {}
                    for key in val:
                        res[key] = json_helper(val[key])
                    return res
                else:
                    return util.json_friendly(val)[0]

            for row in data:
                mapped_row = []
                for v in row:
                    mapped_row.append(json_helper(v))
                mapped_data.append(mapped_row)
            json_dict.update(
                {
                    "_type": Table.artifact_type,
                    "columns": self.columns,
                    "data": mapped_data,
                    "ncols": len(self.columns),
                    "nrows": len(mapped_data),
                    "column_types": self._column_types.to_json(artifact),
                }
            )
        else:
            raise ValueError("to_json accepts wandb_run.Run or wandb_artifact.Artifact")

        return json_dict

    def iterrows(self):
        """Iterate over rows as (ndx, row)
        Yields
        ------
        index : int
            The index of the row.
        row : List[any]
            The data of the row
        """
        for ndx in range(len(self.data)):
            yield ndx, self.data[ndx]


class _PartitionTablePartEntry:
    """Helper class for PartitionTable to track its parts
    """

    def __init__(self, entry, source_artifact):
        self.entry = entry
        self.source_artifact = source_artifact
        self._part = None

    def get_part(self):
        if self._part is None:
            self._part = self.source_artifact.get(self.entry.path)
        return self._part

    def free(self):
        self._part = None


class PartitionedTable(Media):
    """ PartitionedTable represents a table which is composed
    by the union of multiple sub-tables. Currently, PartitionedTable
    is designed to point to a directory within an artifact.
    """

    artifact_type = "partitioned-table"

    def __init__(self, parts_path):
        """
        Args:
            parts_path (str): path to a directory of tables in the artifact
        """
        super(PartitionedTable, self).__init__()
        self.parts_path = parts_path
        self._loaded_part_entries = {}

    def to_json(self, artifact):
        json_obj = super(PartitionedTable, self).to_json(artifact)
        json_obj["parts_path"] = self.parts_path
        return json_obj

    @classmethod
    def from_json(cls, json_obj, source_artifact):
        instance = cls(json_obj["parts_path"])
        entries = source_artifact.manifest.get_entries_in_directory(
            json_obj["parts_path"]
        )
        for entry in entries:
            instance._add_part_entry(entry, source_artifact)
        return instance

    def iterrows(self):
        """Iterate over rows as (ndx, row)
        Yields
        ------
        index : int
            The index of the row.
        row : List[any]
            The data of the row
        """
        columns = None
        ndx = 0
        for entry_path in self._loaded_part_entries:
            part = self._loaded_part_entries[entry_path].get_part()
            if columns is None:
                columns = part.columns
            elif columns != part.columns:
                raise ValueError(
                    "Table parts have non-matching columns. {} != {}".format(
                        columns, part.columns
                    )
                )
            for _, row in part.iterrows():
                yield ndx, row
                ndx += 1

            self._loaded_part_entries[entry_path].free()

    def _add_part_entry(self, entry, source_artifact):
        self._loaded_part_entries[entry.path] = _PartitionTablePartEntry(
            entry, source_artifact
        )

    def __ne__(self, other):
        return not self.__eq__(other)

    def __eq__(self, other):
        return isinstance(other, self.__class__) and self.parts_path == other.parts_path


class Audio(BatchableMedia):
    """
    Wandb class for audio clips.

    Arguments:
        data_or_path: (string or numpy array) A path to an audio file
            or a numpy array of audio data.
        sample_rate: (int) Sample rate, required when passing in raw
            numpy array of audio data.
        caption: (string) Caption to display with audio.
    """

    artifact_type = "audio-file"

    def __init__(self, data_or_path, sample_rate=None, caption=None):
        """Accepts a path to an audio file or a numpy array of audio data."""
        super(Audio, self).__init__()
        self._duration = None
        self._sample_rate = sample_rate
        self._caption = caption

        if isinstance(data_or_path, six.string_types):
            self._set_file(data_or_path, is_tmp=False)
        else:
            if sample_rate is None:
                raise ValueError(
                    'Argument "sample_rate" is required when instantiating wandb.Audio with raw data.'
                )

            soundfile = util.get_module(
                "soundfile",
                required='Raw audio requires the soundfile package. To get it, run "pip install soundfile"',
            )

            tmp_path = os.path.join(MEDIA_TMP.name, util.generate_id() + ".wav")
            soundfile.write(tmp_path, data_or_path, sample_rate)
            self._duration = len(data_or_path) / float(sample_rate)

            self._set_file(tmp_path, is_tmp=True)

    @classmethod
    def get_media_subdir(cls):
        return os.path.join("media", "audio")

    @classmethod
    def from_json(cls, json_obj, source_artifact):
        return cls(
            source_artifact.get_path(json_obj["path"]).download(),
            json_obj["sample_rate"],
            json_obj["caption"],
        )

    def to_json(self, run):
        json_dict = super(Audio, self).to_json(run)
        json_dict.update(
            {
                "_type": self.artifact_type,
                "sample_rate": self._sample_rate,
                "caption": self._caption,
            }
        )
        return json_dict

    @classmethod
    def seq_to_json(cls, seq, run, key, step):
        audio_list = list(seq)

        util.get_module(
            "soundfile",
            required="wandb.Audio requires the soundfile package. To get it, run: pip install soundfile",
        )
        base_path = os.path.join(run.dir, "media", "audio")
        util.mkdir_exists_ok(base_path)
        meta = {
            "_type": "audio",
            "count": len(audio_list),
            "audio": [a.to_json(run) for a in audio_list],
        }
        sample_rates = cls.sample_rates(audio_list)
        if sample_rates:
            meta["sampleRates"] = sample_rates
        durations = cls.durations(audio_list)
        if durations:
            meta["durations"] = durations
        captions = cls.captions(audio_list)
        if captions:
            meta["captions"] = captions

        return meta

    @classmethod
    def durations(cls, audio_list):
        return [a._duration for a in audio_list]

    @classmethod
    def sample_rates(cls, audio_list):
        return [a._sample_rate for a in audio_list]

    @classmethod
    def captions(cls, audio_list):
        captions = [a._caption for a in audio_list]
        if all(c is None for c in captions):
            return False
        else:
            return ["" if c is None else c for c in captions]

    def __eq__(self, other):
        return (
            super(Audio, self).__eq__(other)
            and self._sample_rate == other._sample_rate
            and self._caption == other._caption
        )

    def __ne__(self, other):
        return not self.__eq__(other)


def is_numpy_array(data):
    np = util.get_module(
        "numpy", required="Logging raw point cloud data requires numpy"
    )
    return isinstance(data, np.ndarray)


class Classes(Media):
    artifact_type = "classes"

    def __init__(self, class_set):
        """Classes is holds class metadata intended to be used in concert with other objects when visualizing artifacts

        Args:
            class_set (list): list of dicts in the form of {"id":int|str, "name":str}
        """
        super(Classes, self).__init__()
        for class_obj in class_set:
            assert "id" in class_obj and "name" in class_obj
        self._class_set = class_set

    @classmethod
    def from_json(cls, json_obj, source_artifact):
        return cls(json_obj.get("class_set"))

    def to_json(self, artifact=None):
        json_obj = {}
        # This is a bit of a hack to allow _ClassesIdType to
        # be able to operate fully without an artifact in play.
        # In all other cases, artifact should be a true artifact.
        if artifact is not None:
            json_obj = super(Classes, self).to_json(artifact)
        json_obj["_type"] = Classes.artifact_type
        json_obj["class_set"] = self._class_set
        return json_obj

    def get_type(self):
        return _ClassesIdType(self)

    def __ne__(self, other):
        return not self.__eq__(other)

    def __eq__(self, other):
        return self._class_set == other._class_set


class JoinedTable(Media):
    """Joins two tables for visualization in the Artifact UI

    Arguments:
        table1 (str, wandb.Table, ArtifactEntry):
            the path to a wandb.Table in an artifact, the table object, or ArtifactEntry
        table2 (str, wandb.Table):
            the path to a wandb.Table in an artifact, the table object, or ArtifactEntry
        join_key (str, [str, str]):
            key or keys to perform the join
    """

    artifact_type = "joined-table"

    def __init__(self, table1, table2, join_key):
        super(JoinedTable, self).__init__()

        if not isinstance(join_key, str) and (
            not isinstance(join_key, list) or len(join_key) != 2
        ):
            raise ValueError(
                "JoinedTable join_key should be a string or a list of two strings"
            )

        if not self._validate_table_input(table1):
            raise ValueError(
                "JoinedTable table1 should be an artifact path to a table or wandb.Table object"
            )

        if not self._validate_table_input(table2):
            raise ValueError(
                "JoinedTable table2 should be an artifact path to a table or wandb.Table object"
            )

        self._table1 = table1
        self._table2 = table2
        self._join_key = join_key

    @classmethod
    def from_json(cls, json_obj, source_artifact):
        t1 = source_artifact.get(json_obj["table1"])
        if t1 is None:
            t1 = json_obj["table1"]

        t2 = source_artifact.get(json_obj["table2"])
        if t2 is None:
            t2 = json_obj["table2"]

        return cls(t1, t2, json_obj["join_key"],)

    @staticmethod
    def _validate_table_input(table):
        """Helper method to validate that the table input is one of the 3 supported types"""
        return (
            (type(table) == str and table.endswith(".table.json"))
            or isinstance(table, Table)
            or isinstance(table, PartitionedTable)
            or (hasattr(table, "ref_url") and table.ref_url().endswith(".table.json"))
        )

    def _ensure_table_in_artifact(self, table, artifact, table_ndx):
        """Helper method to add the table to the incoming artifact. Returns the path"""
        if isinstance(table, Table) or isinstance(table, PartitionedTable):
            table_name = "t{}_{}".format(table_ndx, str(id(self)))
            if (
                table.artifact_source is not None
                and table.artifact_source.name is not None
            ):
                table_name = os.path.basename(table.artifact_source.name)
            entry = artifact.add(table, table_name)
            table = entry.path
        # Check if this is an ArtifactEntry
        elif hasattr(table, "ref_url"):
            # Give the new object a unique, yet deterministic name
            name = binascii.hexlify(
                base64.standard_b64decode(table.entry.digest)
            ).decode("ascii")[:8]
            entry = artifact.add_reference(
                table.ref_url(), "{}.{}.json".format(name, table.name.split(".")[-2])
            )[0]
            table = entry.path

        err_str = "JoinedTable table:{} not found in artifact. Add a table to the artifact using Artifact#add(<table>, {}) before adding this JoinedTable"
        if table not in artifact._manifest.entries:
            raise ValueError(err_str.format(table, table))

        return table

    def to_json(self, artifact):
        json_obj = super(JoinedTable, self).to_json(artifact)

        table1 = self._ensure_table_in_artifact(self._table1, artifact, 1)
        table2 = self._ensure_table_in_artifact(self._table2, artifact, 2)

        json_obj.update(
            {
                "_type": JoinedTable.artifact_type,
                "table1": table1,
                "table2": table2,
                "join_key": self._join_key,
            }
        )
        return json_obj

    def __ne__(self, other):
        return not self.__eq__(other)

    def __eq__(self, other):
        return (
            self._table1 == other._table1
            and self._table2 == other._table2
            and self._join_key == other._join_key
        )


class Image(BatchableMedia):
    """
    Wandb class for images.

    Arguments:
        data_or_path: (numpy array, string, io) Accepts numpy array of
            image data, or a PIL image. The class attempts to infer
            the data format and converts it.
        mode: (string) The PIL mode for an image. Most common are "L", "RGB",
            "RGBA". Full explanation at https://pillow.readthedocs.io/en/4.2.x/handbook/concepts.html#concept-modes.
        caption: (string) Label for display of image.
    """

    MAX_ITEMS = 108

    # PIL limit
    MAX_DIMENSION = 65500

    artifact_type = "image-file"

    def __init__(
        self,
        data_or_path,
        mode=None,
        caption=None,
        grouping=None,
        classes=None,
        boxes=None,
        masks=None,
    ):
        super(Image, self).__init__()
        # TODO: We should remove grouping, it's a terrible name and I don't
        # think anyone uses it.

        self._grouping = None
        self._caption = None
        self._width = None
        self._height = None
        self._image = None
        self._classes = None
        self._boxes = None
        self._masks = None

        # Allows the user to pass an Image object as the first parameter and have a perfect copy,
        # only overriding additional metdata passed in. If this pattern is compelling, we can generalize.
        if isinstance(data_or_path, Image):
            self._initialize_from_wbimage(data_or_path)
        elif isinstance(data_or_path, six.string_types):
            self._initialize_from_path(data_or_path)
        else:
            self._initialize_from_data(data_or_path, mode)

        self._set_initialization_meta(grouping, caption, classes, boxes, masks)

    def _set_initialization_meta(
        self, grouping=None, caption=None, classes=None, boxes=None, masks=None
    ):
        if grouping is not None:
            self._grouping = grouping

        if caption is not None:
            self._caption = caption

        if classes is not None:
            if not isinstance(classes, Classes):
                self._classes = Classes(classes)
            else:
                self._classes = classes

        if boxes:
            if not isinstance(boxes, dict):
                raise ValueError('Images "boxes" argument must be a dictionary')
            boxes_final = {}
            for key in boxes:
                if isinstance(boxes[key], BoundingBoxes2D):
                    boxes_final[key] = boxes[key]
                else:
                    boxes_final[key] = BoundingBoxes2D(boxes[key], key)
            self._boxes = boxes_final

        if masks:
            if not isinstance(masks, dict):
                raise ValueError('Images "masks" argument must be a dictionary')
            masks_final = {}
            for key in masks:
                if isinstance(masks[key], ImageMask):
                    masks_final[key] = masks[key]
                else:
                    masks_final[key] = ImageMask(masks[key], key)
            self._masks = masks_final

        self._width, self._height = self._image.size

    def _initialize_from_wbimage(self, wbimage):
        self._grouping = wbimage._grouping
        self._caption = wbimage._caption
        self._width = wbimage._width
        self._height = wbimage._height
        self._image = wbimage._image
        self._classes = wbimage._classes
        self._path = wbimage._path
        self._is_tmp = wbimage._is_tmp
        self._extension = wbimage._extension
        self._sha256 = wbimage._sha256
        self._size = wbimage._size
        self.format = wbimage.format
        self.artifact_source = wbimage.artifact_source

        # We do not want to implicitly copy boxes or masks, just the image-related data.
        # self._boxes = wbimage._boxes
        # self._masks = wbimage._masks

    def _initialize_from_path(self, path):
        pil_image = util.get_module(
            "PIL.Image",
            required='wandb.Image needs the PIL package. To get it, run "pip install pillow".',
        )
        self._set_file(path, is_tmp=False)
        self._image = pil_image.open(path)
        self._image.load()
        ext = os.path.splitext(path)[1][1:]
        self.format = ext

    def _initialize_from_data(self, data, mode=None):
        pil_image = util.get_module(
            "PIL.Image",
            required='wandb.Image needs the PIL package. To get it, run "pip install pillow".',
        )
        if util.is_matplotlib_typename(util.get_full_typename(data)):
            buf = six.BytesIO()
            util.ensure_matplotlib_figure(data).savefig(buf)
            self._image = pil_image.open(buf)
        elif isinstance(data, pil_image.Image):
            self._image = data
        elif util.is_pytorch_tensor_typename(util.get_full_typename(data)):
            vis_util = util.get_module(
                "torchvision.utils", "torchvision is required to render images"
            )
            if hasattr(data, "requires_grad") and data.requires_grad:
                data = data.detach()
            data = vis_util.make_grid(data, normalize=True)
            self._image = pil_image.fromarray(
                data.mul(255).clamp(0, 255).byte().permute(1, 2, 0).cpu().numpy()
            )
        else:
            if hasattr(data, "numpy"):  # TF data eager tensors
                data = data.numpy()
            if data.ndim > 2:
                data = data.squeeze()  # get rid of trivial dimensions as a convenience
            self._image = pil_image.fromarray(
                self.to_uint8(data), mode=mode or self.guess_mode(data)
            )

        tmp_path = os.path.join(MEDIA_TMP.name, util.generate_id() + ".png")
        self.format = "png"
        self._image.save(tmp_path, transparency=None)
        self._set_file(tmp_path, is_tmp=True)

    @classmethod
    def from_json(cls, json_obj, source_artifact):
        classes = None
        if json_obj.get("classes") is not None:
            classes = source_artifact.get(json_obj["classes"]["path"])

        _masks = None
        masks = json_obj.get("masks")
        if masks:
            _masks = {}
            for key in masks:
                _masks[key] = ImageMask.from_json(masks[key], source_artifact)
                _masks[key].set_artifact_source(source_artifact)
                _masks[key]._key = key

        boxes = json_obj.get("boxes")
        _boxes = None
        if boxes:
            _boxes = {}
            for key in boxes:
                _boxes[key] = BoundingBoxes2D.from_json(boxes[key], source_artifact)
                _boxes[key]._key = key

        return cls(
            source_artifact.get_path(json_obj["path"]).download(),
            caption=json_obj.get("caption"),
            grouping=json_obj.get("grouping"),
            classes=classes,
            boxes=_boxes,
            masks=_masks,
        )

    @classmethod
    def get_media_subdir(cls):
        return os.path.join("media", "images")

    def bind_to_run(self, *args, **kwargs):
        super(Image, self).bind_to_run(*args, **kwargs)
        id_ = kwargs.get("id_")
        if self._boxes is not None:
            for i, k in enumerate(self._boxes):
                kwargs["id_"] = "{}{}".format(id_, i) if id_ is not None else None
                self._boxes[k].bind_to_run(*args, **kwargs)

        if self._masks is not None:
            for i, k in enumerate(self._masks):
                kwargs["id_"] = "{}{}".format(id_, i) if id_ is not None else None
                self._masks[k].bind_to_run(*args, **kwargs)

    def to_json(self, run_or_artifact):
        json_dict = super(Image, self).to_json(run_or_artifact)
        json_dict["_type"] = Image.artifact_type
        json_dict["format"] = self.format

        if self._width is not None:
            json_dict["width"] = self._width
        if self._height is not None:
            json_dict["height"] = self._height
        if self._grouping:
            json_dict["grouping"] = self._grouping
        if self._caption:
            json_dict["caption"] = self._caption

        wandb_run, wandb_artifacts = _safe_sdk_import()

        if isinstance(run_or_artifact, wandb_artifacts.Artifact):
            artifact = run_or_artifact
            if (
                self._masks is not None or self._boxes is not None
            ) and self._classes is None:
                raise ValueError(
                    "classes must be passed to wandb.Image which have masks or bounding boxes when adding to artifacts"
                )

            if self._classes is not None:
                # Here, rather than give each class definition it's own name (and entry), we
                # purposely are giving a non-unique class name of /media/cls.classes.json.
                # This may create user confusion if if multiple different class definitions
                # are expected in a single artifact. However, we want to catch this user pattern
                # if it exists and dive deeper. The alternative code is provided below.
                #
                class_name = os.path.join("media", "cls")
                #
                # class_name = os.path.join(
                #     "media", "classes", os.path.basename(self._path) + "_cls"
                # )
                #
                classes_entry = artifact.add(self._classes, class_name)
                json_dict["classes"] = {
                    "type": "classes-file",
                    "path": classes_entry.path,
                    "digest": classes_entry.digest,
                }

        elif not isinstance(run_or_artifact, wandb_run.Run):
            raise ValueError("to_json accepts wandb_run.Run or wandb_artifact.Artifact")

        if self._boxes:
            json_dict["boxes"] = {
                k: box.to_json(run_or_artifact) for (k, box) in self._boxes.items()
            }
        if self._masks:
            json_dict["masks"] = {
                k: mask.to_json(run_or_artifact) for (k, mask) in self._masks.items()
            }
        return json_dict

    def guess_mode(self, data):
        """
        Guess what type of image the np.array is representing
        """
        # TODO: do we want to support dimensions being at the beginning of the array?
        if data.ndim == 2:
            return "L"
        elif data.shape[-1] == 3:
            return "RGB"
        elif data.shape[-1] == 4:
            return "RGBA"
        else:
            raise ValueError(
                "Un-supported shape for image conversion %s" % list(data.shape)
            )

    @classmethod
    def to_uint8(cls, data):
        """
        Converts floating point image on the range [0,1] and integer images
        on the range [0,255] to uint8, clipping if necessary.
        """
        np = util.get_module(
            "numpy",
            required="wandb.Image requires numpy if not supplying PIL Images: pip install numpy",
        )

        # I think it's better to check the image range vs the data type, since many
        # image libraries will return floats between 0 and 255

        # some images have range -1...1 or 0-1
        dmin = np.min(data)
        if dmin < 0:
            data = (data - np.min(data)) / np.ptp(data)
        if np.max(data) <= 1.0:
            data = (data * 255).astype(np.int32)

        # assert issubclass(data.dtype.type, np.integer), 'Illegal image format.'
        return data.clip(0, 255).astype(np.uint8)

    @classmethod
    def seq_to_json(cls, images, run, key, step):
        """
        Combines a list of images into a meta dictionary object describing the child images.
        """

        jsons = [obj.to_json(run) for obj in images]

        media_dir = cls.get_media_subdir()

        for obj in jsons:
            expected = util.to_forward_slash_path(media_dir)
            if not obj["path"].startswith(expected):
                raise ValueError(
                    "Files in an array of Image's must be in the {} directory, not {}".format(
                        cls.get_media_subdir(), obj["path"]
                    )
                )

        num_images_to_log = len(images)
        width, height = images[0]._image.size
        format = jsons[0]["format"]

        def size_equals_image(image):
            img_width, img_height = image._image.size
            return img_width == width and img_height == height

        sizes_match = all(size_equals_image(img) for img in images)
        if not sizes_match:
            logging.warning(
                "Images sizes do not match. This will causes images to be display incorrectly in the UI."
            )

        meta = {
            "_type": "images/separated",
            "width": width,
            "height": height,
            "format": format,
            "count": num_images_to_log,
        }

        captions = Image.all_captions(images)

        if captions:
            meta["captions"] = captions

        all_masks = Image.all_masks(images, run, key, step)

        if all_masks:
            meta["all_masks"] = all_masks

        all_boxes = Image.all_boxes(images, run, key, step)

        if all_boxes:
            meta["all_boxes"] = all_boxes

        return meta

    @classmethod
    def all_masks(cls, images, run, run_key, step):
        all_mask_groups = []
        for image in images:
            if image._masks:
                mask_group = {}
                for k in image._masks:
                    mask = image._masks[k]
                    mask_group[k] = mask.to_json(run)
                all_mask_groups.append(mask_group)
            else:
                all_mask_groups.append(None)
        if all_mask_groups and not all(x is None for x in all_mask_groups):
            return all_mask_groups
        else:
            return False

    @classmethod
    def all_boxes(cls, images, run, run_key, step):
        all_box_groups = []
        for image in images:
            if image._boxes:
                box_group = {}
                for k in image._boxes:
                    box = image._boxes[k]
                    box_group[k] = box.to_json(run)
                all_box_groups.append(box_group)
            else:
                all_box_groups.append(None)
        if all_box_groups and not all(x is None for x in all_box_groups):
            return all_box_groups
        else:
            return False

    @classmethod
    def all_captions(cls, images):
        if images[0]._caption is not None:
            return [i._caption for i in images]
        else:
            return False

    def __ne__(self, other):
        return not self.__eq__(other)

    def __eq__(self, other):
        return (
            self._grouping == other._grouping
            and self._caption == other._caption
            and self._width == other._width
            and self._height == other._height
            and self._image == other._image
            and self._classes == other._classes
        )


# Allows encoding of arbitrary JSON structures
# as a file
#
# This class should be used as an abstract class
# extended to have validation methods


class JSONMetadata(Media):
    """
    JSONMetadata is a type for encoding arbitrary metadata as files.
    """

    def __init__(self, val, **kwargs):
        super(JSONMetadata, self).__init__()

        self.validate(val)
        self._val = val

        ext = "." + self.type_name() + ".json"
        tmp_path = os.path.join(MEDIA_TMP.name, util.generate_id() + ext)
        util.json_dump_uncompressed(
            self._val, codecs.open(tmp_path, "w", encoding="utf-8")
        )
        self._set_file(tmp_path, is_tmp=True, extension=ext)

    def get_media_subdir(self):
        return os.path.join("media", "metadata", self.type_name())

    def to_json(self, run):
        json_dict = super(JSONMetadata, self).to_json(run)
        json_dict["_type"] = self.type_name()

        return json_dict

    # These methods should be overridden in the child class
    def type_name(self):
        return "metadata"

    def validate(self, val):
        return True


class BoundingBoxes2D(JSONMetadata):
    """
    Wandb class for 2D bounding boxes
    """

    artifact_type = "bounding-boxes"

    def __init__(self, val, key, **kwargs):
        """
        Args:
            val (dict): dictionary following the form:
            {
                "class_labels": optional mapping from class ids to strings {id: str}
                "box_data": list of boxes: [
                    {
                        "position": {
                            "minX": float,
                            "maxX": float,
                            "minY": float,
                            "maxY": float,
                        },
                        "class_id": 1,
                        "box_caption": optional str
                        "scores": optional dict of scores
                    },
                    ...
                ],
            }
            key (str): id for set of bounding boxes
        """
        super(BoundingBoxes2D, self).__init__(val)
        self._val = val["box_data"]
        self._key = key
        # Add default class mapping
        if "class_labels" not in val:
            np = util.get_module(
                "numpy", required="Semantic Segmentation mask support requires numpy"
            )
            classes = (
                np.unique(list(map(lambda box: box["class_id"], val["box_data"])))
                .astype(np.int32)
                .tolist()
            )
            class_labels = dict((c, "class_" + str(c)) for c in classes)
            self._class_labels = class_labels
        else:
            self._class_labels = val["class_labels"]

    def bind_to_run(self, run, key, step, id_=None):
        # bind_to_run key argument is the Image parent key
        # the self._key value is the mask's sub key
        super(BoundingBoxes2D, self).bind_to_run(run, key, step, id_=id_)
        run._add_singleton(
            "bounding_box/class_labels",
            key + "_wandb_delimeter_" + self._key,
            self._class_labels,
        )

    def type_name(self):
        return "boxes2D"

    def validate(self, val):
        # Optional argument
        if "class_labels" in val:
            for k, v in list(val["class_labels"].items()):
                if (not isinstance(k, numbers.Number)) or (
                    not isinstance(v, six.string_types)
                ):
                    raise TypeError(
                        "Class labels must be a dictionary of numbers to string"
                    )

        boxes = val["box_data"]
        if not isinstance(boxes, Sequence):
            raise TypeError("Boxes must be a list")

        for box in boxes:
            # Required arguments
            error_str = "Each box must contain a position with: middle, width, and height or \
                    \nminX, maxX, minY, maxY."
            if "position" not in box:
                raise TypeError(error_str)
            else:
                valid = False
                if (
                    "middle" in box["position"]
                    and len(box["position"]["middle"]) == 2
                    and has_num(box["position"], "width")
                    and has_num(box["position"], "height")
                ):
                    valid = True
                elif (
                    has_num(box["position"], "minX")
                    and has_num(box["position"], "maxX")
                    and has_num(box["position"], "minY")
                    and has_num(box["position"], "maxY")
                ):
                    valid = True

                if not valid:
                    raise TypeError(error_str)

            # Optional arguments
            if ("scores" in box) and not isinstance(box["scores"], dict):
                raise TypeError("Box scores must be a dictionary")
            elif "scores" in box:
                for k, v in list(box["scores"].items()):
                    if not isinstance(k, six.string_types):
                        raise TypeError("A score key must be a string")
                    if not isinstance(v, numbers.Number):
                        raise TypeError("A score value must be a number")

            if ("class_id" in box) and not isinstance(
                box["class_id"], six.integer_types
            ):
                raise TypeError("A box's class_id must be an integer")

            # Optional
            if ("box_caption" in box) and not isinstance(
                box["box_caption"], six.string_types
            ):
                raise TypeError("A box's caption must be a string")

    def to_json(self, run_or_artifact):
        wandb_run, wandb_artifacts = _safe_sdk_import()

        if isinstance(run_or_artifact, wandb_run.Run):
            return super(BoundingBoxes2D, self).to_json(run_or_artifact)
        elif isinstance(run_or_artifact, wandb_artifacts.Artifact):
            # TODO (tim): I would like to log out a proper dictionary representing this object, but don't
            # want to mess with the visualizations that are currently available in the UI. This really should output
            # an object with a _type key. Will need to push this change to the UI first to ensure backwards compat
            return self._val
        else:
            raise ValueError("to_json accepts wandb_run.Run or wandb_artifact.Artifact")

    @classmethod
    def from_json(cls, json_obj, source_artifact):
        return cls({"box_data": json_obj}, "")


class ImageMask(Media):
    """
    Wandb class for image masks, useful for segmentation tasks
    """

    artifact_type = "mask"

    def __init__(self, val, key, **kwargs):
        """
        Args:
            val (dict): dictionary following 1 of two forms:
            {
                "mask_data": 2d array of integers corresponding to classes,
                "class_labels": optional mapping from class ids to strings {id: str}
            }

            {
                "path": path to an image file containing integers corresponding to classes,
                "class_labels": optional mapping from class ids to strings {id: str}
            }
            key (str): id for set of masks
        """
        super(ImageMask, self).__init__()

        if "path" in val:
            self._set_file(val["path"])
        else:
            np = util.get_module(
                "numpy", required="Semantic Segmentation mask support requires numpy"
            )
            # Add default class mapping
            if "class_labels" not in val:
                classes = np.unique(val["mask_data"]).astype(np.int32).tolist()
                class_labels = dict((c, "class_" + str(c)) for c in classes)
                val["class_labels"] = class_labels

            self.validate(val)
            self._val = val
            self._key = key

            ext = "." + self.type_name() + ".png"
            tmp_path = os.path.join(MEDIA_TMP.name, util.generate_id() + ext)

            pil_image = util.get_module(
                "PIL.Image",
                required='wandb.Image needs the PIL package. To get it, run "pip install pillow".',
            )
            image = pil_image.fromarray(val["mask_data"].astype(np.int8), mode="L")

            image.save(tmp_path, transparency=None)
            self._set_file(tmp_path, is_tmp=True, extension=ext)

    def bind_to_run(self, run, key, step, id_=None):
        # bind_to_run key argument is the Image parent key
        # the self._key value is the mask's sub key
        super(ImageMask, self).bind_to_run(run, key, step, id_=id_)
        class_labels = self._val["class_labels"]

        run._add_singleton(
            "mask/class_labels", key + "_wandb_delimeter_" + self._key, class_labels
        )

    def get_media_subdir(self):
        return os.path.join("media", "images", self.type_name())

    @classmethod
    def from_json(cls, json_obj, source_artifact):
        return cls(
            {"path": source_artifact.get_path(json_obj["path"]).download()}, key="",
        )

    def to_json(self, run_or_artifact):
        json_dict = super(ImageMask, self).to_json(run_or_artifact)
        wandb_run, wandb_artifacts = _safe_sdk_import()

        if isinstance(run_or_artifact, wandb_run.Run):
            json_dict["_type"] = self.type_name()
            return json_dict
        elif isinstance(run_or_artifact, wandb_artifacts.Artifact):
            # Nothing special to add (used to add "digest", but no longer used.)
            return json_dict
        else:
            raise ValueError("to_json accepts wandb_run.Run or wandb_artifact.Artifact")

    def type_name(self):
        return "mask"

    def validate(self, mask):
        np = util.get_module(
            "numpy", required="Semantic Segmentation mask support requires numpy"
        )
        # 2D Make this work with all tensor(like) types
        if "mask_data" not in mask:
            raise TypeError(
                'Missing key "mask_data": A mask requires mask data(A 2D array representing the predctions)'
            )
        else:
            error_str = "mask_data must be a 2d array"
            shape = mask["mask_data"].shape
            if len(shape) != 2:
                raise TypeError(error_str)
            if not (
                (mask["mask_data"] >= 0).all() and (mask["mask_data"] <= 255).all()
            ) and issubclass(mask["mask_data"].dtype.type, np.integer):
                raise TypeError("Mask data must be integers between 0 and 255")

        # Optional argument
        if "class_labels" in mask:
            for k, v in list(mask["class_labels"].items()):
                if (not isinstance(k, numbers.Number)) or (
                    not isinstance(v, six.string_types)
                ):
                    raise TypeError(
                        "Class labels must be a dictionary of numbers to string"
                    )


class Plotly(Media):
    """
    Wandb class for plotly plots.

    Arguments:
        val: matplotlib or plotly figure
    """

    @classmethod
    def make_plot_media(cls, val):
        if util.is_matplotlib_typename(util.get_full_typename(val)):
            if util.matplotlib_contains_images(val):
                return Image(val)
            val = util.matplotlib_to_plotly(val)
        return cls(val)

    def __init__(self, val, **kwargs):
        super(Plotly, self).__init__()
        # First, check to see if the incoming `val` object is a plotfly figure
        if not util.is_plotly_figure_typename(util.get_full_typename(val)):
            # If it is not, but it is a matplotlib figure, then attempt to convert it to plotly
            if util.is_matplotlib_typename(util.get_full_typename(val)):
                if util.matplotlib_contains_images(val):
                    raise ValueError(
                        "Plotly does not currently support converting matplotlib figures containing images. \
                            You can convert the plot to a static image with `wandb.Image(plt)` "
                    )
                val = util.matplotlib_to_plotly(val)
            else:
                raise ValueError(
                    "Logged plots must be plotly figures, or matplotlib plots convertible to plotly via mpl_to_plotly"
                )

        tmp_path = os.path.join(MEDIA_TMP.name, util.generate_id() + ".plotly.json")
        val = numpy_arrays_to_lists(val.to_plotly_json())
        util.json_dump_safer(val, codecs.open(tmp_path, "w", encoding="utf-8"))
        self._set_file(tmp_path, is_tmp=True, extension=".plotly.json")

    def get_media_subdir(self):
        return os.path.join("media", "plotly")

    def to_json(self, run):
        json_dict = super(Plotly, self).to_json(run)
        json_dict["_type"] = "plotly-file"
        return json_dict


class Bokeh(Media):
    """
    Wandb class for Bokeh plots.

    Arguments:
        val: Bokeh plot
    """

    artifact_type = "bokeh-file"

    def __init__(self, data_or_path):
        super(Bokeh, self).__init__()
        bokeh = util.get_module("bokeh", required=True)
        if isinstance(data_or_path, str) and os.path.exists(data_or_path):
            with open(data_or_path, "r") as file:
                b_json = json.load(file)
            self.b_obj = bokeh.document.Document.from_json(b_json)
            self._set_file(data_or_path, is_tmp=False, extension=".bokeh.json")
        elif isinstance(data_or_path, bokeh.model.Model):
            _data = bokeh.document.Document()
            _data.add_root(data_or_path)
            # serialize/deserialize pairing followed by sorting attributes ensures
            # that the file's shas are equivalent in subsequent calls
            self.b_obj = bokeh.document.Document.from_json(_data.to_json())
            b_json = self.b_obj.to_json()
            if "references" in b_json["roots"]:
                b_json["roots"]["references"].sort(key=lambda x: x["id"])

            tmp_path = os.path.join(MEDIA_TMP.name, util.generate_id() + ".bokeh.json")
            util.json_dump_safer(b_json, codecs.open(tmp_path, "w", encoding="utf-8"))
            self._set_file(tmp_path, is_tmp=True, extension=".bokeh.json")
        elif not isinstance(data_or_path, bokeh.document.Document):
            raise TypeError(
                "Bokeh constructor accepts Bokeh document/model or path to Bokeh json file"
            )

    def get_media_subdir(self):
        return os.path.join("media", "bokeh")

    def to_json(self, run):
        # TODO: (tss) this is getting redundant for all the media objects. We can probably
        # pull this into Media#to_json and remove this type override for all the media types.
        # There are only a few cases where the type is different between artifacts and runs.
        json_dict = super(Bokeh, self).to_json(run)
        json_dict["_type"] = self.artifact_type
        return json_dict

    @classmethod
    def from_json(cls, json_obj, source_artifact):
        return cls(source_artifact.get_path(json_obj["path"]).download())


class Graph(Media):
    """Wandb class for graphs

    This class is typically used for saving and diplaying neural net models.  It
    represents the graph as an array of nodes and edges.  The nodes can have
    labels that can be visualized by wandb.

    Examples:
        Import a keras model:
        ```
            Graph.from_keras(keras_model)
        ```

    Attributes:
        format (string): Format to help wandb display the graph nicely.
        nodes ([wandb.Node]): List of wandb.Nodes
        nodes_by_id (dict): dict of ids -> nodes
        edges ([(wandb.Node, wandb.Node)]): List of pairs of nodes interpreted as edges
        loaded (boolean): Flag to tell whether the graph is completely loaded
        root (wandb.Node): root node of the graph
    """

    def __init__(self, format="keras"):
        super(Graph, self).__init__()
        # LB: TODO: I think we should factor criterion and criterion_passed out
        self.format = format
        self.nodes = []
        self.nodes_by_id = {}
        self.edges = []
        self.loaded = False
        self.criterion = None
        self.criterion_passed = False
        self.root = None  # optional root Node if applicable

    def _to_graph_json(self, run=None):
        # Needs to be it's own function for tests
        return {
            "format": self.format,
            "nodes": [node.to_json() for node in self.nodes],
            "edges": [edge.to_json() for edge in self.edges],
        }

    def bind_to_run(self, *args, **kwargs):
        data = self._to_graph_json()
        tmp_path = os.path.join(MEDIA_TMP.name, util.generate_id() + ".graph.json")
        data = numpy_arrays_to_lists(data)
        util.json_dump_safer(data, codecs.open(tmp_path, "w", encoding="utf-8"))
        self._set_file(tmp_path, is_tmp=True, extension=".graph.json")
        if self.is_bound():
            return
        super(Graph, self).bind_to_run(*args, **kwargs)

    @classmethod
    def get_media_subdir(cls):
        return os.path.join("media", "graph")

    def to_json(self, run):
        json_dict = super(Graph, self).to_json(run)
        json_dict["_type"] = "graph-file"
        return json_dict

    def __getitem__(self, nid):
        return self.nodes_by_id[nid]

    def pprint(self):
        for edge in self.edges:
            pprint.pprint(edge.attributes)
        for node in self.nodes:
            pprint.pprint(node.attributes)

    def add_node(self, node=None, **node_kwargs):
        if node is None:
            node = Node(**node_kwargs)
        elif node_kwargs:
            raise ValueError(
                "Only pass one of either node ({node}) or other keyword arguments ({node_kwargs})".format(
                    node=node, node_kwargs=node_kwargs
                )
            )
        self.nodes.append(node)
        self.nodes_by_id[node.id] = node

        return node

    def add_edge(self, from_node, to_node):
        edge = Edge(from_node, to_node)
        self.edges.append(edge)

        return edge

    @classmethod
    def from_keras(cls, model):
        graph = cls()
        # Shamelessly copied (then modified) from keras/keras/utils/layer_utils.py
        sequential_like = cls._is_sequential(model)

        relevant_nodes = None
        if not sequential_like:
            relevant_nodes = []
            for v in model._nodes_by_depth.values():
                relevant_nodes += v

        layers = model.layers
        for i in range(len(layers)):
            node = Node.from_keras(layers[i])
            if hasattr(layers[i], "_inbound_nodes"):
                for in_node in layers[i]._inbound_nodes:
                    if relevant_nodes and in_node not in relevant_nodes:
                        # node is not part of the current network
                        continue
                    for in_layer in nest(in_node.inbound_layers):
                        inbound_keras_node = Node.from_keras(in_layer)

                        if inbound_keras_node.id not in graph.nodes_by_id:
                            graph.add_node(inbound_keras_node)
                        inbound_node = graph.nodes_by_id[inbound_keras_node.id]

                        graph.add_edge(inbound_node, node)
            graph.add_node(node)
        return graph

    @classmethod
    def _is_sequential(cls, model):
        sequential_like = True

        if (
            model.__class__.__name__ != "Sequential"
            and hasattr(model, "_is_graph_network")
            and model._is_graph_network
        ):
            nodes_by_depth = model._nodes_by_depth.values()
            nodes = []
            for v in nodes_by_depth:
                # TensorFlow2 doesn't insure inbound is always a list
                inbound = v[0].inbound_layers
                if not hasattr(inbound, "__len__"):
                    inbound = [inbound]
                if (len(v) > 1) or (len(v) == 1 and len(inbound) > 1):
                    # if the model has multiple nodes
                    # or if the nodes have multiple inbound_layers
                    # the model is no longer sequential
                    sequential_like = False
                    break
                nodes += v
            if sequential_like:
                # search for shared layers
                for layer in model.layers:
                    flag = False
                    if hasattr(layer, "_inbound_nodes"):
                        for node in layer._inbound_nodes:
                            if node in nodes:
                                if flag:
                                    sequential_like = False
                                    break
                                else:
                                    flag = True
                    if not sequential_like:
                        break
        return sequential_like


class Node(WBValue):
    """
    Node used in `Graph`
    """

    def __init__(
        self,
        id=None,
        name=None,
        class_name=None,
        size=None,
        parameters=None,
        output_shape=None,
        is_output=None,
        num_parameters=None,
        node=None,
    ):
        self._attributes = {"name": None}
        self.in_edges = {}  # indexed by source node id
        self.out_edges = {}  # indexed by dest node id
        # optional object (eg. PyTorch Parameter or Module) that this Node represents
        self.obj = None

        if node is not None:
            self._attributes.update(node._attributes)
            del self._attributes["id"]
            self.obj = node.obj

        if id is not None:
            self.id = id
        if name is not None:
            self.name = name
        if class_name is not None:
            self.class_name = class_name
        if size is not None:
            self.size = size
        if parameters is not None:
            self.parameters = parameters
        if output_shape is not None:
            self.output_shape = output_shape
        if is_output is not None:
            self.is_output = is_output
        if num_parameters is not None:
            self.num_parameters = num_parameters

    def to_json(self, run=None):
        return self._attributes

    def __repr__(self):
        return repr(self._attributes)

    @property
    def id(self):
        """Must be unique in the graph"""
        return self._attributes.get("id")

    @id.setter
    def id(self, val):
        self._attributes["id"] = val
        return val

    @property
    def name(self):
        """Usually the type of layer or sublayer"""
        return self._attributes.get("name")

    @name.setter
    def name(self, val):
        self._attributes["name"] = val
        return val

    @property
    def class_name(self):
        """Usually the type of layer or sublayer"""
        return self._attributes.get("class_name")

    @class_name.setter
    def class_name(self, val):
        self._attributes["class_name"] = val
        return val

    @property
    def functions(self):
        return self._attributes.get("functions", [])

    @functions.setter
    def functions(self, val):
        self._attributes["functions"] = val
        return val

    @property
    def parameters(self):
        return self._attributes.get("parameters", [])

    @parameters.setter
    def parameters(self, val):
        self._attributes["parameters"] = val
        return val

    @property
    def size(self):
        return self._attributes.get("size")

    @size.setter
    def size(self, val):
        """Tensor size"""
        self._attributes["size"] = tuple(val)
        return val

    @property
    def output_shape(self):
        return self._attributes.get("output_shape")

    @output_shape.setter
    def output_shape(self, val):
        """Tensor output_shape"""
        self._attributes["output_shape"] = val
        return val

    @property
    def is_output(self):
        return self._attributes.get("is_output")

    @is_output.setter
    def is_output(self, val):
        """Tensor is_output"""
        self._attributes["is_output"] = val
        return val

    @property
    def num_parameters(self):
        return self._attributes.get("num_parameters")

    @num_parameters.setter
    def num_parameters(self, val):
        """Tensor num_parameters"""
        self._attributes["num_parameters"] = val
        return val

    @property
    def child_parameters(self):
        return self._attributes.get("child_parameters")

    @child_parameters.setter
    def child_parameters(self, val):
        """Tensor child_parameters"""
        self._attributes["child_parameters"] = val
        return val

    @property
    def is_constant(self):
        return self._attributes.get("is_constant")

    @is_constant.setter
    def is_constant(self, val):
        """Tensor is_constant"""
        self._attributes["is_constant"] = val
        return val

    @classmethod
    def from_keras(cls, layer):
        node = cls()

        try:
            output_shape = layer.output_shape
        except AttributeError:
            output_shape = ["multiple"]

        node.id = layer.name
        node.name = layer.name
        node.class_name = layer.__class__.__name__
        node.output_shape = output_shape
        node.num_parameters = layer.count_params()

        return node


class Edge(WBValue):
    """
    Edge used in `Graph`
    """

    def __init__(self, from_node, to_node):
        self._attributes = {}
        self.from_node = from_node
        self.to_node = to_node

    def __repr__(self):
        temp_attr = dict(self._attributes)
        del temp_attr["from_node"]
        del temp_attr["to_node"]
        temp_attr["from_id"] = self.from_node.id
        temp_attr["to_id"] = self.to_node.id
        return str(temp_attr)

    def to_json(self, run=None):
        return [self.from_node.id, self.to_node.id]

    @property
    def name(self):
        """Optional, not necessarily unique"""
        return self._attributes.get("name")

    @name.setter
    def name(self, val):
        self._attributes["name"] = val
        return val

    @property
    def from_node(self):
        return self._attributes.get("from_node")

    @from_node.setter
    def from_node(self, val):
        self._attributes["from_node"] = val
        return val

    @property
    def to_node(self):
        return self._attributes.get("to_node")

    @to_node.setter
    def to_node(self, val):
        self._attributes["to_node"] = val
        return val


def nest(thing):
    # Use tensorflows nest function if available, otherwise just wrap object in an array"""

    tfutil = util.get_module("tensorflow.python.util")
    if tfutil:
        return tfutil.nest.flatten(thing)
    else:
        return [thing]


def history_dict_to_json(run, payload, step=None):
    # Converts a History row dict's elements so they're friendly for JSON serialization.

    if step is None:
        # We should be at the top level of the History row; assume this key is set.
        step = payload["_step"]

    # We use list here because we were still seeing cases of RuntimeError dict changed size
    for key in list(payload):
        val = payload[key]
        if isinstance(val, dict):
            payload[key] = history_dict_to_json(run, val, step=step)
        else:
            payload[key] = val_to_json(run, key, val, namespace=step)

    return payload


def numpy_arrays_to_lists(payload):
    # Casts all numpy arrays to lists so we don't convert them to histograms, primarily for Plotly

    if isinstance(payload, dict):
        res = {}
        for key, val in six.iteritems(payload):
            res[key] = numpy_arrays_to_lists(val)
        return res
    elif isinstance(payload, Sequence) and not isinstance(payload, six.string_types):
        return [numpy_arrays_to_lists(v) for v in payload]
    elif util.is_numpy_array(payload):
        return [numpy_arrays_to_lists(v) for v in payload.tolist()]

    return payload


def prune_max_seq(seq):
    # If media type has a max respect it
    items = seq
    if hasattr(seq[0], "MAX_ITEMS") and seq[0].MAX_ITEMS < len(seq):
        logging.warning(
            "Only %i %s will be uploaded."
            % (seq[0].MAX_ITEMS, seq[0].__class__.__name__)
        )
        items = seq[: seq[0].MAX_ITEMS]
    return items


def val_to_json(run, key, val, namespace=None):
    # Converts a wandb datatype to its JSON representation.
    if namespace is None:
        raise ValueError(
            "val_to_json must be called with a namespace(a step number, or 'summary') argument"
        )

    converted = val
    typename = util.get_full_typename(val)

    if util.is_pandas_data_frame(val):
        assert namespace == "summary", "We don't yet support DataFrames in History."
        return data_frame_to_json(val, run, key, namespace)
    elif util.is_matplotlib_typename(typename) or util.is_plotly_typename(typename):
        val = Plotly.make_plot_media(val)
    elif isinstance(val, Sequence) and all(isinstance(v, WBValue) for v in val):
        # This check will break down if Image/Audio/... have child classes.
        if (
            len(val)
            and isinstance(val[0], BatchableMedia)
            and all(isinstance(v, type(val[0])) for v in val)
        ):
            items = prune_max_seq(val)

            for i, item in enumerate(items):
                item.bind_to_run(run, key, namespace, id_=i)

            return items[0].seq_to_json(items, run, key, namespace)
        else:
            # TODO(adrian): Good idea to pass on the same key here? Maybe include
            # the array index?
            # There is a bug here: if this array contains two arrays of the same type of
            # anonymous media objects, their eventual names will collide.
            # This used to happen. The frontend doesn't handle heterogenous arrays
            # raise ValueError(
            #    "Mixed media types in the same list aren't supported")
            return [val_to_json(run, key, v, namespace=namespace) for v in val]

    if isinstance(val, WBValue):
        if isinstance(val, Media) and not val.is_bound():
            val.bind_to_run(run, key, namespace)
        return val.to_json(run)

    return converted


def data_frame_to_json(df, run, key, step):
    """!NODOC Encode a Pandas DataFrame into the JSON/backend format.

    Writes the data to a file and returns a dictionary that we use to represent
    it in `Summary`'s.

    Arguments:
        df (pandas.DataFrame): The DataFrame. Must not have columns named
            "wandb_run_id" or "wandb_data_frame_id". They will be added to the
            DataFrame here.
        run (wandb_run.Run): The Run the DataFrame is associated with. We need
            this because the information we store on the DataFrame is derived
            from the Run it's in.
        key (str): Name of the DataFrame, ie. the summary key path in which it's
            stored. This is for convenience, so people exploring the
            directory tree can have some idea of what is in the Parquet files.
        step: History step or "summary".

    Returns:
        A dict representing the DataFrame that we can store in summaries or
        histories. This is the format:
        {
            '_type': 'data-frame',
                # Magic field that indicates that this object is a data frame as
                # opposed to a normal dictionary or anything else.
            'id': 'asdf',
                # ID for the data frame that is unique to this Run.
            'format': 'parquet',
                # The file format in which the data frame is stored. Currently can
                # only be Parquet.
            'project': 'wfeas',
                # (Current) name of the project that this Run is in. It'd be
                # better to store the project's ID because we know it'll never
                # change but we don't have that here. We store this just in
                # case because we use the project name in identifiers on the
                # back end.
            'path': 'media/data_frames/sdlk.parquet',
                # Path to the Parquet file in the Run directory.
        }
    """
    pandas = util.get_module("pandas")
    fastparquet = util.get_module("fastparquet")
    missing_reqs = []
    if not pandas:
        missing_reqs.append("pandas")
    if not fastparquet:
        missing_reqs.append("fastparquet")
    if len(missing_reqs) > 0:
        raise wandb.Error(
            "Failed to save data frame. Please run 'pip install %s'"
            % " ".join(missing_reqs)
        )

    data_frame_id = util.generate_id()

    df = df.copy()  # we don't want to modify the user's DataFrame instance.

    for _, series in df.items():
        for i, val in enumerate(series):
            if isinstance(val, WBValue):
                series.iat[i] = six.text_type(
                    json.dumps(val_to_json(run, key, val, namespace=step))
                )

    # We have to call this wandb_run_id because that name is treated specially by
    # our filtering code
    df["wandb_run_id"] = pandas.Series(
        [six.text_type(run.id)] * len(df.index), index=df.index
    )

    df["wandb_data_frame_id"] = pandas.Series(
        [six.text_type(data_frame_id)] * len(df.index), index=df.index
    )
    frames_dir = os.path.join(run.dir, DATA_FRAMES_SUBDIR)
    util.mkdir_exists_ok(frames_dir)
    path = os.path.join(frames_dir, "{}-{}.parquet".format(key, data_frame_id))
    fastparquet.write(path, df)

    return {
        "id": data_frame_id,
        "_type": "data-frame",
        "format": "parquet",
        "project": run.project_name(),  # we don't have the project ID here
        "entity": run.entity,
        "run": run.id,
        "path": path,
    }


# Custom dtypes for typing system


class _ClassesIdType(_dtypes.Type):
    name = "wandb.Classes_id"
    types = [Classes]

    def __init__(
        self, classes_obj=None, valid_ids=None,
    ):
        if valid_ids is None:
            valid_ids = _dtypes.UnionType()
        elif isinstance(valid_ids, list):
            valid_ids = _dtypes.UnionType(
                [_dtypes.ConstType(item) for item in valid_ids]
            )
        elif isinstance(valid_ids, _dtypes.UnionType):
            valid_ids = valid_ids
        else:
            raise TypeError("valid_ids must be None, list, or UnionType")

        if classes_obj is None:
            classes_obj = Classes(
                [
                    {"id": _id.params["val"], "name": str(_id.params["val"])}
                    for _id in valid_ids.params["allowed_types"]
                ]
            )
        elif not isinstance(classes_obj, Classes):
            raise TypeError("valid_ids must be None, or instance of Classes")
        else:
            valid_ids = _dtypes.UnionType(
                [
                    _dtypes.ConstType(class_obj["id"])
                    for class_obj in classes_obj._class_set
                ]
            )

        self.wb_classes_obj_ref = classes_obj
        self.params.update({"valid_ids": valid_ids})

    def assign(self, py_obj=None):
        return self.assign_type(_dtypes.ConstType(py_obj))

    def assign_type(self, wb_type=None):
        valid_ids = self.params["valid_ids"].assign_type(wb_type)
        if not isinstance(valid_ids, _dtypes.InvalidType):
            return self

        return _dtypes.InvalidType()

    @classmethod
    def from_obj(cls, py_obj=None):
        return cls(py_obj)

    def to_json(self, artifact=None):
        cl_dict = super(_ClassesIdType, self).to_json(artifact)
        # TODO (tss): Refactor this block with the similar one in wandb.Image.
        # This is a bit of a smell that the classes object does not follow
        # the same file-pattern as other media types.
        if artifact is not None:
            class_name = os.path.join("media", "cls")
            classes_entry = artifact.add(self.wb_classes_obj_ref, class_name)
            cl_dict["params"]["classes_obj"] = {
                "type": "classes-file",
                "path": classes_entry.path,
                "digest": classes_entry.digest,  # is this needed really?
            }
        else:
            cl_dict["params"]["classes_obj"] = self.wb_classes_obj_ref.to_json(artifact)
        return cl_dict

    @classmethod
    def from_json(cls, json_dict, artifact=None):
        classes_obj = None
        if (
            json_dict.get("params", {}).get("classes_obj", {}).get("type")
            == "classes-file"
        ):
            classes_obj = artifact.get(
                json_dict.get("params", {}).get("classes_obj", {}).get("path")
            )
        else:
            classes_obj = Classes.from_json(
                json_dict["params"]["classes_obj"], artifact
            )

        return cls(classes_obj)


class _ImageType(_dtypes.Type):
    name = "wandb.Image"
    types = [Image]

    def __init__(self, box_keys=None, mask_keys=None):
        if box_keys is None:
            box_keys = _dtypes.UnknownType()
        elif isinstance(box_keys, _dtypes.ConstType):
            box_keys = box_keys
        elif not isinstance(box_keys, list):
            raise TypeError("box_keys must be a list")
        else:
            box_keys = _dtypes.ConstType(set(box_keys))

        if mask_keys is None:
            mask_keys = _dtypes.UnknownType()
        elif isinstance(mask_keys, _dtypes.ConstType):
            mask_keys = mask_keys
        elif not isinstance(mask_keys, list):
            raise TypeError("mask_keys must be a list")
        else:
            mask_keys = _dtypes.ConstType(set(mask_keys))

        self.params.update(
            {"box_keys": box_keys, "mask_keys": mask_keys,}
        )

    def assign_type(self, wb_type=None):
        if isinstance(wb_type, _ImageType):
            box_keys = self.params["box_keys"].assign_type(wb_type.params["box_keys"])
            mask_keys = self.params["mask_keys"].assign_type(
                wb_type.params["mask_keys"]
            )
            if not (
                isinstance(box_keys, _dtypes.InvalidType)
                or isinstance(mask_keys, _dtypes.InvalidType)
            ):
                return _ImageType(box_keys, mask_keys)

        return _dtypes.InvalidType()

    @classmethod
    def from_obj(cls, py_obj):
        if not isinstance(py_obj, Image):
            raise TypeError("py_obj must be a wandb.Image")
        else:
            if hasattr(py_obj, "_boxes") and py_obj._boxes:
                box_keys = list(py_obj._boxes.keys())
            else:
                box_keys = []

            if hasattr(py_obj, "masks") and py_obj.masks:
                mask_keys = list(py_obj.masks.keys())
            else:
                mask_keys = []

            return cls(box_keys, mask_keys)


class _TableType(_dtypes.Type):
    name = "wandb.Table"
    types = [Table]

    def __init__(self, column_types=None):
        if column_types is None:
            column_types = _dtypes.UnknownType()
        if isinstance(column_types, dict):
            column_types = _dtypes.DictType(column_types)
        elif not (
            isinstance(column_types, _dtypes.DictType)
            or isinstance(column_types, _dtypes.UnknownType)
        ):
            raise TypeError("column_types must be a dict or DictType")

        self.params.update({"column_types": column_types})

    def assign_type(self, wb_type=None):
        if isinstance(wb_type, _TableType):
            column_types = self.params["column_types"].assign_type(
                wb_type.params["column_types"]
            )
            if not isinstance(column_types, _dtypes.InvalidType):
                return _TableType(column_types)

        return _dtypes.InvalidType()

    @classmethod
    def from_obj(cls, py_obj):
        if not isinstance(py_obj, Table):
            raise TypeError("py_obj must be a wandb.Table")
        else:
            return cls(py_obj._column_types)


_dtypes.TypeRegistry.add(_ClassesIdType)
_dtypes.TypeRegistry.add(_ImageType)
_dtypes.TypeRegistry.add(_TableType)
