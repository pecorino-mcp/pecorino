"""
Export module for Pecorino.

Provides JSON and YAML export functionality for all collected metrics.
"""

import datetime
import json
import logging
import os

logger = logging.getLogger(__name__)

from typing import Any, Dict, Optional

from src.core.config import conf


class DateTimeEncoder(json.JSONEncoder):
    """Custom JSON encoder that handles datetime objects."""

    def default(self, obj):
        if isinstance(obj, datetime.datetime):
            return obj.isoformat()
        if isinstance(obj, datetime.timedelta):
            return str(obj)
        if isinstance(obj, set):
            return list(obj)
        if hasattr(obj, '__dict__'):
            return str(obj)
        return super().default(obj)


class MetricsCollector:
    """
    Collects and formats various repository metrics from the data collector.
    """

    def __init__(self, data_collector, hotspot_data: Optional[Dict] = None):
        self.data = data_collector
        self.hotspot_data = hotspot_data or {}

    def collect_all(self) -> Dict[str, Any]:
        """Collect all metrics from the data collector into a structured dict."""
        return {
            'metadata': self._get_metadata(),
            'repository': self._get_repository_stats(),
            'activity': self._get_activity_stats(),
            'authors': self._get_author_stats(),
            'files': self._get_file_stats(),
            'code_quality': self._get_code_quality_stats(),
            'hotspots': self._get_hotspot_stats(),
            'branches': self._get_branch_stats(),
        }

    def _get_metadata(self) -> Dict[str, Any]:
        """Get export metadata."""
        return {
            'generated_at': datetime.datetime.now().isoformat(),
            'pecorino_version': '3.0',
            'repository_name': getattr(self.data, 'projectname', 'unknown'),
            'repository_path': getattr(self.data, 'repository_path', 'unknown'),
        }

    def _get_repository_stats(self) -> Dict[str, Any]:
        """Get repository-level statistics."""
        data = self.data

        first_commit = None
        last_commit = None

        try:
            if hasattr(data, 'first_commit_stamp') and data.first_commit_stamp:
                first_commit = datetime.datetime.fromtimestamp(data.first_commit_stamp).isoformat()
            if hasattr(data, 'last_commit_stamp') and data.last_commit_stamp:
                last_commit = datetime.datetime.fromtimestamp(data.last_commit_stamp).isoformat()
        except (OSError, ValueError):
            pass

        repo_stats = getattr(data, 'repository_stats', {})

        return {
            'total_commits': data.getTotalCommits() if hasattr(data, 'getTotalCommits') else 0,
            'total_files': data.getTotalFiles() if hasattr(data, 'getTotalFiles') else 0,
            'total_lines': data.getTotalLOC() if hasattr(data, 'getTotalLOC') else 0,
            'total_source_lines': data.getTotalSourceLines() if hasattr(data, 'getTotalSourceLines') else 0,
            'total_authors': data.getTotalAuthors() if hasattr(data, 'getTotalAuthors') else 0,
            'first_commit': first_commit,
            'last_commit': last_commit,
            'active_days': len(data.getActiveDays()) if hasattr(data, 'getActiveDays') else 0,
            'lines_added': repo_stats.get('total_lines_added', 0),
            'lines_removed': repo_stats.get('total_lines_removed', 0),
        }

    def _get_activity_stats(self) -> Dict[str, Any]:
        """Get activity statistics."""
        data = self.data

        activity = {
            'by_hour_of_day': {},
            'by_day_of_week': {},
            'by_month': {},
            'last_30_days': {},
            'pace_of_changes': {},
        }

        # Hour of day
        if hasattr(data, 'activity_by_hour_of_day'):
            hour_data = data.activity_by_hour_of_day
            if isinstance(hour_data, dict):
                activity['by_hour_of_day'] = {str(k): v for k, v in hour_data.items()}

        # Day of week
        if hasattr(data, 'activity_by_day_of_week'):
            dow_data = data.activity_by_day_of_week
            if isinstance(dow_data, dict):
                day_names = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
                activity['by_day_of_week'] = {
                    day_names[k] if isinstance(k, int) and 0 <= k < 7 else str(k): v
                    for k, v in dow_data.items()
                }

        # Last 30 days
        if hasattr(data, 'getLast30DaysActivity'):
            activity['last_30_days'] = data.getLast30DaysActivity()

        # Pace of changes by month
        if hasattr(data, 'getPaceOfChangesByMonth'):
            activity['pace_of_changes']['by_month'] = data.getPaceOfChangesByMonth()

        return activity

    def _get_author_stats(self) -> Dict[str, Any]:
        """Get author statistics."""
        data = self.data
        authors_data = {}

        if hasattr(data, 'authors') and data.authors:
            for author, info in data.authors.items():
                if isinstance(info, dict):
                    authors_data[author] = {
                        'commits': info.get('commits', 0),
                        'lines_added': info.get('lines_added', 0),
                        'lines_removed': info.get('lines_removed', 0),
                        'first_commit': info.get('date_first', ''),
                        'last_commit': info.get('date_last', ''),
                        'contribution_percentage': info.get('commits_frac', 0),
                    }

        # Get team performance if available
        team_performance = {}
        if hasattr(data, 'team_performance') and data.team_performance:
            for author, perf in data.team_performance.items():
                if isinstance(perf, dict):
                    team_performance[author] = {
                        'activity_score': perf.get('activity_score', 0),
                        'consistency': perf.get('consistency', 0),
                        'collaboration_score': perf.get('collaboration_score', 0),
                        'overall_score': perf.get('overall_score', 0),
                    }

        return {
            'authors': authors_data,
            'team_performance': team_performance,
            'bus_factor': self._calculate_bus_factor(authors_data),
        }

    def _calculate_bus_factor(self, authors_data: Dict) -> int:
        """Calculate the bus factor (minimum authors for 50% of commits)."""
        if not authors_data:
            return 0

        sorted_authors = sorted(
            authors_data.items(),
            key=lambda x: x[1].get('commits', 0),
            reverse=True
        )

        total_commits = sum(a[1].get('commits', 0) for a in sorted_authors)
        if total_commits == 0:
            return 0

        cumulative = 0
        bus_factor = 0

        for _, info in sorted_authors:
            cumulative += info.get('commits', 0)
            bus_factor += 1
            if cumulative >= total_commits * 0.5:
                break

        return bus_factor

    def _get_file_stats(self) -> Dict[str, Any]:
        """Get file statistics."""
        data = self.data

        files = {
            'by_extension': {},
            'largest_files': [],
            'most_revised': [],
        }

        # SLOC by extension
        if hasattr(data, 'getSLOCByExtension'):
            sloc_ext = data.getSLOCByExtension()
            if isinstance(sloc_ext, dict):
                files['by_extension'] = dict(sloc_ext)

        # Largest files
        if hasattr(data, 'getLargestFiles'):
            largest = data.getLargestFiles(10)
            files['largest_files'] = [
                {'path': f[0], 'size_bytes': f[1]} for f in largest
            ]

        # Most revised files
        if hasattr(data, 'getFilesWithMostRevisions'):
            most_revised = data.getFilesWithMostRevisions(10)
            files['most_revised'] = [
                {'path': f[0], 'revisions': f[1]} for f in most_revised
            ]

        return files

    def _get_code_quality_stats(self) -> Dict[str, Any]:
        """Get code quality statistics."""
        data = self.data

        quality = {
            'maintainability_summary': {},
            'complexity_summary': {},
            'oop_metrics': {},
        }

        # Comprehensive metrics
        if hasattr(data, 'comprehensive_metrics'):
            cm = data.comprehensive_metrics
            if isinstance(cm, dict):
                quality['maintainability_summary'] = cm.get('maintainability_summary', {})
                quality['total_complexity'] = cm.get('total_complexity', 0)
                quality['average_mi'] = cm.get('average_mi', 0)

        # OOP metrics
        if hasattr(data, 'oop_metrics'):
            oop = data.oop_metrics
            if isinstance(oop, dict):
                quality['oop_metrics'] = {
                    'total_classes': oop.get('total_classes', 0),
                    'abstract_classes': oop.get('abstract_classes', 0),
                    'abstractness': oop.get('abstractness', 0),
                    'instability': oop.get('instability', 0),
                    'distance_from_main_sequence': oop.get('distance', 0),
                }

        return quality

    def _get_hotspot_stats(self) -> Dict[str, Any]:
        """Get hotspot analysis results."""
        if not self.hotspot_data:
            return {'hotspots': [], 'summary': {}}

        hotspots = self.hotspot_data.get('hotspots', [])
        summary = self.hotspot_data.get('summary', {})

        # Serialize hotspots (limit to top 20)
        serialized_hotspots = []
        for h in hotspots[:20]:
            serialized_hotspots.append({
                'filepath': h.get('filepath', ''),
                'risk_score': h.get('risk_score', 0),
                'risk_level': h.get('risk_level', 'unknown'),
                'churn_score': h.get('churn_score', 0),
                'complexity_score': h.get('complexity_score', 0),
                'revisions': h.get('revisions', 0),
                'coupling_count': h.get('coupling_count', 0),
            })

        return {
            'hotspots': serialized_hotspots,
            'summary': summary,
        }

    def _get_branch_stats(self) -> Dict[str, Any]:
        """Get branch statistics."""
        data = self.data

        branches = {
            'total_branches': 0,
            'unmerged_count': 0,
            'main_branch': '',
            'branches': {},
        }

        if hasattr(data, 'getBranches'):
            all_branches = data.getBranches()
            branches['total_branches'] = len(all_branches)

            # Serialize branch data (limit detail)
            for name, info in list(all_branches.items())[:20]:
                if isinstance(info, dict):
                    branches['branches'][name] = {
                        'commits': info.get('commits', 0),
                        'lines_added': info.get('lines_added', 0),
                        'lines_removed': info.get('lines_removed', 0),
                    }

        if hasattr(data, 'getUnmergedBranches'):
            branches['unmerged_count'] = len(data.getUnmergedBranches())

        if hasattr(data, 'getMainBranch'):
            branches['main_branch'] = data.getMainBranch()

        return branches


class MetricsExporter:
    """
    Exports repository metrics to JSON or YAML formats.
    """

    def __init__(self, data_collector, hotspot_data: Optional[Dict] = None):
        """
        Initialize the exporter.
        
        Args:
            data_collector: A DataCollector instance with collected repository data
            hotspot_data: Optional hotspot analysis results
        """
        self.collector = MetricsCollector(data_collector, hotspot_data)

    def export_json(self, output_path: str, pretty: bool = True) -> str:
        """
        Export all metrics to a JSON file.
        
        Args:
            output_path: Directory to write the JSON file
            pretty: Whether to format with indentation
            
        Returns:
            Path to the created JSON file
        """
        metrics = self.collector.collect_all()

        filepath = os.path.join(output_path, 'pecorino_metrics.json')

        with open(filepath, 'w', encoding='utf-8') as f:
            if pretty:
                json.dump(metrics, f, cls=DateTimeEncoder, indent=2, ensure_ascii=False)
            else:
                json.dump(metrics, f, cls=DateTimeEncoder, ensure_ascii=False)

        if conf.get('verbose'):
            logger.info(f'Exported metrics to {filepath}')

        return filepath

    def export_yaml(self, output_path: str) -> str:
        """
        Export all metrics to a YAML file.
        
        Args:
            output_path: Directory to write the YAML file
            
        Returns:
            Path to the created YAML file, or empty string if PyYAML not available
        """
        try:
            import yaml
        except ImportError:
            if conf.get('verbose'):
                logger.info('Warning: PyYAML not installed, skipping YAML export')
            return ''

        metrics = self.collector.collect_all()

        filepath = os.path.join(output_path, 'pecorino_metrics.yaml')

        with open(filepath, 'w', encoding='utf-8') as f:
            yaml.dump(metrics, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

        if conf.get('verbose'):
            logger.info(f'Exported metrics to {filepath}')

        return filepath

    def get_metrics_dict(self) -> Dict[str, Any]:
        """
        Get all metrics as a dictionary (for programmatic access).
        
        Returns:
            Dictionary containing all metrics
        """
        return self.collector.collect_all()


def export_to_json(data_collector, output_path: str, hotspot_data: Optional[Dict] = None) -> str:
    """
    Convenience function to export metrics to JSON.
    
    Args:
        data_collector: A DataCollector instance
        output_path: Directory to write the JSON file
        hotspot_data: Optional hotspot analysis results
        
    Returns:
        Path to the created JSON file
    """
    exporter = MetricsExporter(data_collector, hotspot_data)
    return exporter.export_json(output_path)


def export_to_yaml(data_collector, output_path: str, hotspot_data: Optional[Dict] = None) -> str:
    """
    Convenience function to export metrics to YAML.
    
    Args:
        data_collector: A DataCollector instance
        output_path: Directory to write the YAML file
        hotspot_data: Optional hotspot analysis results
        
    Returns:
        Path to the created YAML file
    """
    exporter = MetricsExporter(data_collector, hotspot_data)
    return exporter.export_yaml(output_path)
